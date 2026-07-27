"""Text grounding for RelayAgent.

Two strategies the adapter uses to turn a target label into a tap point:
`_ground_text_via_a11y` (uiautomator-first, precise and free) and `_extract_xy`
(tolerant parser for a VLM grounding model's coordinate output). Split out of
`relay_agent.py`; the grounding system prompt + fenced-JSON regexes live here
because they are part of the same grounding path.
"""

from __future__ import annotations

import json
import re

from loguru import logger

from agents.device import get_backend

_GROUNDING_SYSTEM = (
    "You are a UI grounding model. Given a phone screenshot and a target "
    "element description, return the click point as JSON with normalized "
    "coordinates in [0, 999]. Reply with ONE ```json``` fenced object: "
    '{"x": <int 0-999>, "y": <int 0-999>}. Pick the visible center of the '
    "element. If you cannot find it, reply with "
    '{"x": null, "y": null}.'
)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_FENCE_ANY = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _ground_text_via_a11y(
    target: str, screen_w: int, screen_h: int
) -> tuple[int, int] | None:
    """Dump the normalized a11y tree and find a node whose text / desc /
    resource-id matches `target`. Returns the center of the matching node's
    bounds in screen pixels, or None on miss.

    Match policy (tightest first):
      1. exact text or desc match
      2. substring match (text contains target, or vice versa)
      3. resource-id matches target (full "pkg:id/name" form or bare name)

    All matches are restricted to clickable / focusable / visible nodes when
    possible — falls back to any node if no clickable match exists.
    """
    nodes = get_backend().dump_ui_tree(dump_timeout=8, pull_timeout=5)
    if nodes is None:
        logger.warning(f"a11y grounding unavailable: UI dump failed for {target!r}")
        return None

    def _visible_center(n) -> tuple[int, int] | None:
        c = n.center  # None already filters absent/zero-area bounds
        if c is None:
            return None
        cx, cy = c
        if cx < 0 or cy < 0 or cx > screen_w or cy > screen_h:
            return None
        return c

    def _candidates(predicate) -> list[tuple[int, tuple[int, int]]]:
        out: list[tuple[int, tuple[int, int]]] = []
        for n in nodes:
            if not predicate(n):
                continue
            c = _visible_center(n)
            if c is None:
                continue
            # Prefer clickable / focusable nodes — higher score = better.
            score = 0
            if n.clickable:
                score += 4
            if n.focusable:
                score += 2
            if n.enabled:
                score += 1
            out.append((score, c))
        out.sort(reverse=True)
        return out

    # Tier 1: exact text or desc
    hits = _candidates(lambda n: n.text == target or n.desc == target)
    # Tier 2: substring either way
    if not hits:
        hits = _candidates(
            lambda n: (n.text and target in n.text)
            or (n.desc and target in n.desc)
            or (n.text and n.text in target and len(n.text) > 1)
        )
    # Tier 3: resource-id — cards write either the full "pkg:id/name" form or
    # just the bare name; accept both so a full-form selector doesn't silently
    # degrade to the paid VLM fallback.
    if not hits:
        hits = _candidates(
            lambda n: n.resource_id == target
            or n.resource_id.split("/")[-1] == target
        )
    if not hits:
        logger.info(
            f"a11y dump ok ({len(nodes)} nodes) but no match for {target!r}"
        )
        return None
    logger.info(
        f"a11y hit for {target!r}: bounds-center={hits[0][1]} "
        f"(score={hits[0][0]}, {len(hits)} candidates)"
    )
    return hits[0][1]


def _extract_xy(raw: str) -> tuple[int | None, int | None]:
    """Tolerant extractor for VLM grounding outputs.

    Handles, in order of preference:
      - {"x": <int>, "y": <int>}                                (spec)
      - {"point": [x, y]} / {"bbox": [x1,y1,x2,y2]}             (some VLMs)
      - [{"x": [x, y]}, ...]  (Qwen-VL: 'x' field holds [x, y]) (Qwen-VL)
      - [[x, y]] / [x, y]                                       (raw point)
    Falls back to a regex over the first two integers if all else fails.
    """
    import ast

    # 1. Try the spec-shaped fenced object first. Only return values the
    # caller can actually do arithmetic on (int/float) or the explicit
    # not-found null pair; anything else (e.g. quoted numbers {"x": "512"},
    # a common model drift) falls through to the tolerant path below, which
    # ends in the digit-regex fallback — same as unfenced input.
    m = _JSON_FENCE.search(raw)
    if m:
        try:
            d = json.loads(m.group(1))
        except json.JSONDecodeError:
            d = None
        if isinstance(d, dict) and "x" in d and "y" in d and not isinstance(d["x"], list):
            if isinstance(d["x"], (int, float)) and isinstance(d["y"], (int, float)):
                return d["x"], d["y"]
            if d["x"] is None or d["y"] is None:
                return None, None

    # 2. Otherwise grab whatever is inside any fenced block, or the raw text.
    m2 = _FENCE_ANY.search(raw)
    payload = (m2.group(1) if m2 else raw).strip()

    data = None
    for loader in (json.loads, ast.literal_eval):
        try:
            data = loader(payload)
            break
        except (json.JSONDecodeError, ValueError, SyntaxError):
            continue

    def _unwrap(d):
        if isinstance(d, dict):
            # {"x": int, "y": int}
            if isinstance(d.get("x"), (int, float)) and isinstance(d.get("y"), (int, float)):
                return int(d["x"]), int(d["y"])
            # Qwen-VL: {"x": [x, y]}
            if isinstance(d.get("x"), (list, tuple)) and len(d["x"]) >= 2:
                return int(d["x"][0]), int(d["x"][1])
            # {"point": [x, y]} / {"coordinate": [x, y]}
            for k in ("point", "coordinate", "coordinates", "position", "center"):
                v = d.get(k)
                if isinstance(v, (list, tuple)) and len(v) >= 2:
                    return int(v[0]), int(v[1])
            # {"bbox": [x1, y1, x2, y2]} → center
            for k in ("bbox", "bbox_2d", "box"):
                v = d.get(k)
                if isinstance(v, (list, tuple)) and len(v) >= 4:
                    return int((v[0] + v[2]) / 2), int((v[1] + v[3]) / 2)
            # {"x": null, "y": null} → not found
            if "x" in d and "y" in d and d["x"] is None:
                return None, None
        return None

    if isinstance(data, list) and data:
        head = data[0]
        if isinstance(head, (int, float)) and len(data) >= 2:
            return int(data[0]), int(data[1])
        if isinstance(head, (list, tuple)) and len(head) >= 2:
            return int(head[0]), int(head[1])
        if isinstance(head, dict):
            r = _unwrap(head)
            if r is not None:
                return r
    if isinstance(data, dict):
        r = _unwrap(data)
        if r is not None:
            return r

    # 3. Last-ditch: pull the first two integers out of the text.
    nums = re.findall(r"-?\d+", payload)
    if len(nums) >= 2:
        return int(nums[0]), int(nums[1])
    return None, None
