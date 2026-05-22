"""MobileWorld adapter for AppAgentCards.

Run with:

    APPCARDS_TARGET_APP=com.aliyun.tongyi \\
    mw test "在通义里点一杯蜜雪冰城" \\
        --agent-type /abs/path/AppAgentCards/agents/appcards_agent.py \\
        --model_name anthropic/claude-sonnet-4-5

Design:
- Subclass MobileWorld's MCPAgent so we get its provider-agnostic openai
  client, token accounting, and the model_name plumbing for free.
- One LLM call per task picks a capability + invocation text from the card.
- The rest of the turns walk a deterministic plan: open_app, taps using
  card x_bounds, input_text, submit, optional post-result flow.
- Text-based selectors (input field focus, post-result labels) get one
  small VLM grounding call per occurrence — same multi-model client.
- Honors `handoff_to_user_required`: emits ask_user before the irreversible CTA.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# MobileWorld loads this file via importlib.util.spec_from_file_location, so
# the package directory is NOT on sys.path automatically. Add the repo root
# so the sibling modules under `agents/` resolve as a package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from loguru import logger

from mobile_world.agents.base import MCPAgent
from mobile_world.agents.utils.helpers import pil_to_base64
from mobile_world.runtime.utils.models import JSONAction

from agents.action_planner import Step, build_plan
from agents.capability_router import route_capability
from agents.card_loader import bounds_center, load_card_by_app_id

_TARGET_APP_ENV = "APPCARDS_TARGET_APP"
_MANIFESTS_ENV = "APPCARDS_MANIFESTS"
_DENSITY_ENV = "APPCARDS_TARGET_DENSITY"
_FRESH_CONV_ENV = "APPCARDS_FRESH_CONV"  # set to "0" to disable

_GROUNDING_SYSTEM = (
    "You are a UI grounding model. Given a phone screenshot and a target "
    "element description, return the click point as JSON with normalized "
    "coordinates in [0, 999]. Reply with ONE ```json``` fenced object: "
    '{"x": <int 0-999>, "y": <int 0-999>}. Pick the visible center of the '
    "element. If you cannot find it, reply with "
    '{"x": null, "y": null}.'
)
_REPLY_WATCH_SYSTEM = (
    "You watch an in-app AI assistant render its reply on a phone screen. "
    "Decide whether the assistant has FINISHED responding to the user's most "
    "recent message. Signals that it is still generating: a streaming "
    "cursor, a 'Stop'/'停止生成'/'生成中' button near the input, animated "
    "dots, or rapidly changing text. Signals that it is done: a static "
    "reply with action buttons like 'Copy'/'复制', 'Regenerate'/'重新生成', "
    "thumbs-up/down, or the input field showing 'Send'/the normal "
    "placeholder again. "
    "Reply with ONE ```json``` fenced object: "
    '{"done": <true|false>, "text": "<the assistant\'s reply text verbatim, '
    'or null if you cannot read it>"} . '
    "Keep `text` short (<= 500 chars); summarize tail only if too long."
)
_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_FENCE_ANY = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _ground_text_via_uiautomator(
    target: str, screen_w: int, screen_h: int
) -> tuple[int, int] | None:
    """Dump the current UI via `uiautomator dump` and find a node whose
    text / content-desc / resource-id matches `target`. Returns the center of
    the matching node's bounds in screen pixels, or None on miss.

    Match policy (tightest first):
      1. exact text or content-desc match
      2. substring match (text contains target, or vice versa)
      3. resource-id endswith target

    All matches are restricted to clickable / focusable / visible nodes when
    possible — falls back to any node if no clickable match exists.
    """
    device = os.getenv("APPCARDS_ANDROID_SERIAL")  # optional, for multi-device
    base = ["adb"] + (["-s", device] if device else [])

    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as fh:
        local_xml = fh.name
    remote_xml = "/sdcard/appcards_window_dump.xml"
    try:
        dump = subprocess.run(
            base + ["shell", "uiautomator", "dump", remote_xml],
            capture_output=True, text=True, timeout=8,
        )
        if dump.returncode != 0:
            logger.warning(f"uiautomator dump failed: {dump.stderr.strip()}")
            return None
        pull = subprocess.run(
            base + ["pull", remote_xml, local_xml],
            capture_output=True, text=True, timeout=5,
        )
        if pull.returncode != 0 or not os.path.getsize(local_xml):
            logger.warning(f"adb pull failed: {pull.stderr.strip()}")
            return None
        try:
            root = ET.parse(local_xml).getroot()
        except ET.ParseError as e:
            logger.warning(f"window dump XML parse error: {e}")
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning(f"uiautomator path unavailable: {e}")
        return None
    finally:
        try:
            os.unlink(local_xml)
        except OSError:
            pass

    nodes = list(root.iter("node"))

    def _bounds_center(node) -> tuple[int, int] | None:
        m = _BOUNDS_RE.match(node.get("bounds") or "")
        if not m:
            return None
        x1, y1, x2, y2 = (int(v) for v in m.groups())
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        # Filter zero-area or off-screen rectangles.
        if x2 <= x1 or y2 <= y1:
            return None
        if cx < 0 or cy < 0 or cx > screen_w or cy > screen_h:
            return None
        return cx, cy

    def _candidates(predicate) -> list[tuple[int, tuple[int, int]]]:
        out: list[tuple[int, tuple[int, int]]] = []
        for n in nodes:
            if not predicate(n):
                continue
            c = _bounds_center(n)
            if c is None:
                continue
            # Prefer clickable / focusable nodes — higher score = better.
            score = 0
            if (n.get("clickable") or "").lower() == "true":
                score += 4
            if (n.get("focusable") or "").lower() == "true":
                score += 2
            if (n.get("enabled") or "").lower() == "true":
                score += 1
            out.append((score, c))
        out.sort(reverse=True)
        return out

    def _attr(n, k):
        return n.get(k) or ""

    # Tier 1: exact text or content-desc
    hits = _candidates(
        lambda n: _attr(n, "text") == target or _attr(n, "content-desc") == target
    )
    # Tier 2: substring either way
    if not hits:
        hits = _candidates(
            lambda n: (target in _attr(n, "text") and _attr(n, "text"))
            or (target in _attr(n, "content-desc") and _attr(n, "content-desc"))
            or (_attr(n, "text") and _attr(n, "text") in target and len(_attr(n, "text")) > 1)
        )
    # Tier 3: resource-id endswith
    if not hits:
        hits = _candidates(
            lambda n: _attr(n, "resource-id").split("/")[-1] == target
        )
    if not hits:
        logger.info(
            f"uiautomator dump ok ({len(nodes)} nodes) but no match for "
            f"{target!r}"
        )
        return None
    logger.info(
        f"uiautomator hit for {target!r}: bounds-center={hits[0][1]} "
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

    # 1. Try the spec-shaped fenced object first.
    m = _JSON_FENCE.search(raw)
    if m:
        try:
            d = json.loads(m.group(1))
            if isinstance(d, dict) and "x" in d and "y" in d and not isinstance(d["x"], list):
                return d["x"], d["y"]
        except json.JSONDecodeError:
            pass

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


class AppCardsAgent(MCPAgent):
    """Card-driven agent. The model only picks capabilities and grounds text
    selectors; tap coordinates come from the card's `x_bounds`."""

    def __init__(
        self,
        model_name: str,
        llm_base_url: str,
        api_key: str = "empty",
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(tools=tools or [], **kwargs)
        self.model_name = model_name
        self.llm_base_url = llm_base_url
        self.api_key = api_key
        self.build_openai_client(self.llm_base_url, self.api_key)

        self.target_app: str | None = os.getenv(_TARGET_APP_ENV)
        self.manifests_dir = (
            Path(os.environ[_MANIFESTS_ENV])
            if os.getenv(_MANIFESTS_ENV)
            else None
        )
        self.target_density: int | None = (
            int(os.environ[_DENSITY_ENV]) if os.getenv(_DENSITY_ENV) else None
        )

        self.card: dict | None = None
        self.plan: list[Step] = []
        self.cursor: int = 0
        self._planned: bool = False
        self._reply_polls: int = 0
        self._last_agent_reply: str | None = None
        self.fresh_conversation: bool = os.getenv(_FRESH_CONV_ENV, "1") != "0"

    def initialize_hook(self, instruction: str) -> None:
        logger.info(f"AppCardsAgent init: instruction={instruction!r}")
        if not self.target_app:
            raise RuntimeError(
                f"{_TARGET_APP_ENV} must be set to the target app's package id "
                "(e.g. com.aliyun.tongyi)."
            )
        self.card = load_card_by_app_id(self.target_app, self.manifests_dir)
        logger.info(
            f"Loaded card {self.card['app_id']} v{self.card.get('card_version')} "
            f"({self.card['app_name']})"
        )
        self.plan = []
        self.cursor = 0
        self._planned = False

    def reset(self) -> None:
        self.card = None
        self.plan = []
        self.cursor = 0
        self._planned = False
        self._reply_polls = 0
        self._last_agent_reply = None

    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        screenshot = observation["screenshot"]
        screen_w, screen_h = screenshot.size

        if not self._planned:
            cap_id, invocation = route_capability(self, self.instruction, self.card)
            self.plan = build_plan(
                self.card,
                cap_id,
                invocation,
                fresh_conversation=self.fresh_conversation,
            )
            self._planned = True
            logger.info(
                f"Plan ({len(self.plan)} steps) for capability={cap_id!r}: "
                + " → ".join(f"{s.kind}" for s in self.plan)
            )

        if self.cursor >= len(self.plan):
            return ("plan exhausted", JSONAction(action_type="status", goal_status="complete"))

        step = self.plan[self.cursor]
        action, advance, extra_note = self._materialize(step, screenshot, screen_w, screen_h)
        if advance:
            self.cursor += 1
        note = step.note + (f"; {extra_note}" if extra_note else "")
        thought = f"step {self.cursor + (0 if advance else 0)}/{len(self.plan)}: {step.kind} ({note})"
        logger.info(f"{thought} → {action.model_dump(exclude_none=True)}")
        return thought, action

    def _materialize(
        self,
        step: Step,
        screenshot,
        screen_w: int,
        screen_h: int,
    ) -> tuple[JSONAction, bool, str]:
        """Return (action, advance_cursor, extra_note)."""
        kind = step.kind
        p = step.payload

        if kind == "open_app":
            # MobileWorld's open_app expects the launcher label (e.g. "千问"),
            # not the package id. Prefer the card's embedded_agent.name as the
            # launcher label; fall back to app_name, then the package id.
            launcher_label = (
                (self.card or {}).get("embedded_agent", {}).get("name")
                or (self.card or {}).get("app_name")
                or p["package"]
            )
            return JSONAction(action_type="open_app", app_name=launcher_label), True, ""

        if kind == "tap_bounds":
            x, y = bounds_center(
                p["bounds"], self.card, (screen_w, screen_h), self.target_density
            )
            return JSONAction(action_type="click", x=x, y=y), True, ""

        if kind == "tap_fraction":
            return JSONAction(
                action_type="click",
                x=int(p["x_ratio"] * screen_w),
                y=int(p["y_ratio"] * screen_h),
            ), True, ""

        if kind == "tap_text":
            # 1. Try uiautomator XML first (precise, free, robust to UI redraws).
            #    Retry briefly to absorb animation latency (drawer open, etc.).
            # 2. Fall back to the VLM only if the text was not in the a11y tree.
            import time

            xy = None
            for attempt in range(3):
                xy = _ground_text_via_uiautomator(p["text"], screen_w, screen_h)
                if xy is not None:
                    break
                if attempt < 2:
                    time.sleep(0.8)
            if xy is not None:
                x, y = xy
                note = "uiautomator"
            else:
                x, y = self._ground_text(p["text"], screenshot, screen_w, screen_h)
                note = "VLM"
            return JSONAction(action_type="click", x=x, y=y), True, note

        if kind == "input_text":
            return JSONAction(action_type="input_text", text=p["text"], clear_text=True), True, ""

        if kind == "wait_ms":
            return JSONAction(action_type="wait"), True, ""

        if kind == "wait_text":
            return JSONAction(action_type="wait"), True, ""

        if kind == "wait_for_reply":
            done, text = self._poll_agent_reply(screenshot)
            self._reply_polls += 1
            max_polls = max(1, int(p.get("max_seconds", 30)))  # ~1s per poll
            # Trust `done` only if the VLM also produced text. If text is None,
            # the VLM is telling us it cannot read any reply on screen — which
            # almost always means generation has not actually finished. Keep
            # polling until we either get text or hit the timeout.
            if done and text:
                self._last_agent_reply = text
                logger.info(
                    f"In-app agent reply DONE after {self._reply_polls} poll(s); "
                    f"text={text!r}"
                )
                self._reply_polls = 0
                return JSONAction(action_type="wait"), True, f"done; text={text!r}"
            if done and not text:
                logger.warning(
                    f"VLM reported done but returned no text on poll "
                    f"{self._reply_polls}/{max_polls} — distrusting, continuing"
                )
            if self._reply_polls >= max_polls:
                logger.warning(
                    f"In-app agent reply did not finish within {max_polls} polls; "
                    f"advancing anyway (last text={text!r})"
                )
                self._last_agent_reply = text
                self._reply_polls = 0
                return JSONAction(action_type="wait"), True, "timeout"
            return JSONAction(action_type="wait"), False, f"poll {self._reply_polls}/{max_polls}"

        if kind == "swipe":
            return JSONAction(action_type="scroll", direction=p.get("direction", "down")), True, ""

        if kind == "handoff":
            reply_note = (
                f"\n\nAgent reply captured:\n{self._last_agent_reply}"
                if self._last_agent_reply
                else ""
            )
            return JSONAction(
                action_type="ask_user",
                text=(
                    f"Handing control back: {p.get('reason', '')}. The in-app "
                    "agent has surfaced the result; please review and confirm "
                    "any irreversible action yourself."
                    f"{reply_note}"
                ),
            ), True, ""

        if kind == "done":
            return JSONAction(action_type="status", goal_status=p.get("status", "complete")), True, ""

        logger.warning(f"Unsupported step kind={kind}; emitting ask_user")
        return JSONAction(
            action_type="ask_user",
            text=f"Card step not supported by adapter: {kind} {p}",
        ), True, ""

    def _ground_text(
        self,
        target: str,
        screenshot,
        screen_w: int,
        screen_h: int,
    ) -> tuple[int, int]:
        b64 = pil_to_base64(screenshot)
        messages = [
            {"role": "system", "content": _GROUNDING_SYSTEM},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Click on the UI element matching: {target!r}",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            },
        ]
        raw = self.openai_chat_completions_create(
            model=self.model_name,
            messages=messages,
            temperature=0.0,
            max_tokens=128,
        )
        if not raw:
            raise RuntimeError(f"Grounding LLM returned empty for {target!r}")

        rx, ry = _extract_xy(raw)
        logger.info(
            f"Grounding {target!r} on {screen_w}x{screen_h}: raw={raw!r} "
            f"-> extracted=({rx},{ry})"
        )
        if rx is None or ry is None:
            raise RuntimeError(f"Grounding model could not find {target!r}")
        # Detect coordinate-system: if either value clearly exceeds the 0-999
        # normalized range, treat as absolute pixels in the source image.
        if rx > 999 or ry > 999:
            px, py = int(rx), int(ry)
        else:
            px, py = int(rx * screen_w / 999), int(ry * screen_h / 999)
        logger.info(f"Grounding {target!r}: mapped to pixel ({px},{py})")
        return px, py

    def _poll_agent_reply(self, screenshot) -> tuple[bool, str | None]:
        """Ask the VLM whether the in-app assistant has finished replying,
        and capture the reply text. Returns (done, text)."""
        b64 = pil_to_base64(screenshot)
        messages = [
            {"role": "system", "content": _REPLY_WATCH_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Has the in-app assistant finished its reply?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            },
        ]
        raw = self.openai_chat_completions_create(
            model=self.model_name,
            messages=messages,
            temperature=0.0,
            max_tokens=600,
        )
        if not raw:
            logger.warning("Reply-watch LLM returned empty; treating as 'not done'")
            return False, None

        m = _JSON_FENCE.search(raw)
        payload = m.group(1) if m else raw
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            import ast

            try:
                data = ast.literal_eval(payload)
            except (ValueError, SyntaxError):
                logger.warning(f"Reply-watch unparseable response: {raw!r}")
                return False, None
        if not isinstance(data, dict):
            return False, None
        done = bool(data.get("done"))
        text = data.get("text")
        if isinstance(text, str):
            text = text.strip() or None
        else:
            text = None
        return done, text
