"""Matrix-backed three-stage app/capability router.

This is the reusable version of the single-app NL routing strategy:
vertical capabilities first, then app/capability rerank, then a structurally
separate foundation_llm fallback.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from loguru import logger
from openai import OpenAI

from agents.card_catalog import clean_text
from agents.locale_policy import first_locale, locale_policy_text

REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX_CSV = REPO_ROOT / "docs" / "app_capability_matrix.csv"

# The generic, catch-all capability. Routing isolates it into its own fallback
# stage: vertical capabilities are matched first, and `foundation_llm` is only
# considered when nothing vertical fits.
FOUNDATION_CAP = "foundation_llm"

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_HEADER_APPID_RE = re.compile(r"\(([^)]+)\)\s*$")


def parse_fenced_json(text: str) -> dict[str, Any]:
    m = _FENCE_RE.search(text or "")
    payload = m.group(1) if m else text
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data).__name__}")
    return data


def load_matrix(path: Path = MATRIX_CSV) -> dict[str, Any]:
    """Parse docs/app_capability_matrix.csv as the authoritative cap x app map."""
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        raise SystemExit(f"empty capability matrix: {path}")

    header = rows[0]
    app_cols: list[tuple[int, str]] = []
    for idx in range(3, len(header)):
        m = _HEADER_APPID_RE.search(header[idx])
        if not m:
            raise SystemExit(f"matrix header column {header[idx]!r} lacks an (app_id)")
        app_cols.append((idx, m.group(1).strip()))

    cap_desc: dict[str, str] = {}
    cap_to_apps: dict[str, list[str]] = {}
    for row in rows[1:]:
        if len(row) < 3 or not row[1].strip():
            continue
        cap_id = row[1].strip()
        cap_desc[cap_id] = clean_text(row[2])
        apps = [
            app_id
            for idx, app_id in app_cols
            if idx < len(row) and row[idx].strip()
        ]
        cap_to_apps[cap_id] = apps

    return {
        "cap_desc": cap_desc,
        "cap_to_apps": cap_to_apps,
        "app_ids": [app_id for _, app_id in app_cols],
    }


def _llm_json(llm: OpenAI, model: str, system: str, user: str) -> dict[str, Any]:
    resp = llm.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        max_tokens=512,
    )
    raw = (resp.choices[0].message.content or "").strip()
    logger.debug(f"router raw reply: {raw}")
    return parse_fenced_json(raw)


def _catalog_index(catalog: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Map (app_id, capability_id) -> manifest digest for rerank context."""
    idx: dict[tuple[str, str], dict[str, Any]] = {}
    for app in catalog["apps"]:
        for cap in app["capabilities"]:
            idx[(app["app_id"], cap["id"])] = {
                "app_name": app["app_name"],
                "locale": app.get("locale") or [],
                "description": cap.get("description", ""),
                "examples": cap.get("examples", []),
                "executable": cap.get("executable", True),
                "handoff_to_user_required": cap.get("handoff_to_user_required", False),
            }
    return idx


def _candidate_apps_for_cap(
    cap_id: str,
    matrix: dict[str, Any],
    cat_index: dict[tuple[str, str], dict[str, Any]],
) -> list[str]:
    """Return matrix-authorized runnable apps for a capability.

    The matrix is the source of truth for app/capability membership. The
    manifest catalog is only used as an availability check so stale matrix
    entries do not produce unrunnable pairs.
    """
    out: list[str] = []
    stale_matrix_apps: list[str] = []
    for app_id in matrix["cap_to_apps"].get(cap_id, []):
        if (app_id, cap_id) in cat_index:
            out.append(app_id)
        else:
            stale_matrix_apps.append(app_id)
    if stale_matrix_apps:
        logger.warning(
            f"matrix lists non-catalog apps for {cap_id}: {stale_matrix_apps}"
        )
    return out


def _option_for_pair(
    app_id: str,
    cap_id: str,
    matrix: dict[str, Any],
    cat_index: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    digest = cat_index.get((app_id, cap_id)) or {}
    return {
        "app_id": app_id,
        "capability_id": cap_id,
        "app_name": digest.get("app_name", app_id),
        "locale": digest.get("locale") or [],
        "locale_policy": locale_policy_text(first_locale(digest)),
        "description": digest.get("description") or matrix["cap_desc"].get(cap_id, ""),
        "examples": digest.get("examples", []),
        "executable": digest.get("executable", True),
        "handoff_to_user_required": digest.get(
            "handoff_to_user_required", False
        ),
    }


_STAGE1_SYSTEM = """You match a user's request to candidate mobile-app capabilities.

You are given a menu of vertical capability ids with descriptions. A generic
foundation/LLM capability (QA, chat, translation, summarization, open-ended
help) is deliberately NOT in this menu.

Select up to 3 capability ids whose function could fulfill the request, best
first. Use ONLY ids from the menu - never invent one. If NONE of these vertical
capabilities genuinely fit (i.e. the request is generic and better served by a
general assistant), return an empty list.

Return ONE JSON object inside a ```json``` fence:
  {"capability_ids": ["<id>", ...], "reason": "<one short sentence>"}
"""


def _stage1_prefilter(
    nl: str, matrix: dict[str, Any], llm: OpenAI, model: str
) -> list[str]:
    menu = {
        cap_id: desc
        for cap_id, desc in matrix["cap_desc"].items()
        if cap_id != FOUNDATION_CAP
    }
    user = (
        "Vertical capability menu (id: description):\n"
        f"{json.dumps(menu, ensure_ascii=False, indent=2)}\n\n"
        f"User request:\n{nl}\n\n"
        "Return the candidate JSON now."
    )
    data = _llm_json(llm, model, _STAGE1_SYSTEM, user)
    raw_ids = data.get("capability_ids") or []
    seen: set[str] = set()
    out: list[str] = []
    for cid in raw_ids:
        if (
            isinstance(cid, str)
            and cid in matrix["cap_desc"]
            and cid != FOUNDATION_CAP
            and cid not in seen
        ):
            seen.add(cid)
            out.append(cid)
    logger.info(f"stage-1 prefilter -> {out} (reason: {data.get('reason')})")
    return out


_STAGE2_SYSTEM = """You route a user's request to exactly ONE app capability.

You are given a shortlist of (app, capability) options with descriptions and
example prompts. Pick the single best option and write the goal sentence to
hand to that app's in-app agent (rewrite the request if it helps).

Locale policy for the goal sentence typed into the in-app agent:
- Default to the selected app's first locale language.
- If the user's request or planned prompt explicitly asks for a different
  language, honor that explicit instruction.
- Preserve proper nouns, addresses, product names, code, URLs, emails, ids,
  and quoted literal text in their original language.

Use ONLY an app_id + capability_id pair that appears in the options. If, on
reflection, none of the options actually fits the request, return
{"kind": "none"}.

Return ONE JSON object inside a ```json``` fence, either:
  {"kind": "app", "app_id": "<id>", "capability_id": "<id>", "goal": "<sentence>", "reason": "..."}
or:
  {"kind": "none", "reason": "..."}
"""


def _stage2_rerank(
    nl: str,
    cap_ids: list[str],
    matrix: dict[str, Any],
    cat_index: dict[tuple[str, str], dict[str, Any]],
    llm: OpenAI,
    model: str,
) -> dict[str, Any] | None:
    options: list[dict[str, Any]] = []
    for cap_id in cap_ids:
        for app_id in _candidate_apps_for_cap(
            cap_id, matrix, cat_index
        ):
            options.append(_option_for_pair(app_id, cap_id, matrix, cat_index))
    if not options:
        raise RuntimeError(
            "stage-2 early-exit: matched vertical capability ids "
            f"{cap_ids}, but the matrix authorizes no runnable app/capability "
            "pairs for them"
        )
    if len(options) == 1:
        only = options[0]
        logger.info(
            "stage-2 rerank early-exit -> "
            f"{only['app_id']} / {only['capability_id']} (single candidate)"
        )
        return {
            "kind": "app",
            "app_id": only["app_id"],
            "capability_id": only["capability_id"],
            "goal": nl,
            "reason": "Only one runnable app/capability candidate after matrix/catalog reconciliation.",
        }

    user = (
        "Shortlisted (app, capability) options:\n"
        f"{json.dumps(options, ensure_ascii=False, indent=2)}\n\n"
        f"User request:\n{nl}\n\n"
        "Return the routing JSON now."
    )
    data = _llm_json(llm, model, _STAGE2_SYSTEM, user)
    if data.get("kind") == "none":
        logger.info(f"stage-2 rerank -> none (reason: {data.get('reason')})")
        return None
    if data.get("kind") != "app":
        raise RuntimeError(f"stage-2 returned unsupported kind: {data!r}")
    pair = (data.get("app_id"), data.get("capability_id"))
    if pair not in {(o["app_id"], o["capability_id"]) for o in options}:
        logger.warning(f"stage-2 picked off-shortlist pair {pair}; falling back")
        return None
    logger.info(
        f"stage-2 rerank -> {pair[0]} / {pair[1]} (reason: {data.get('reason')})"
    )
    return data


_STAGE3_SYSTEM = """You route a user's request to a general-purpose in-app
assistant (foundation LLM capability) because no specialized vertical
capability fit.

Pick the single best app from the options below and write the goal sentence to
hand to its assistant (rewrite the request if it helps).

Locale policy for the goal sentence typed into the in-app agent:
- Default to the selected app's first locale language.
- If the user's request explicitly asks for a different language, honor that
  explicit instruction.
- Preserve proper nouns, addresses, product names, code, URLs, emails, ids,
  and quoted literal text in their original language.

Return ONE JSON object inside a ```json``` fence:
  {"kind": "app", "app_id": "<id>", "capability_id": "foundation_llm", "goal": "<sentence>", "reason": "..."}
"""


def _stage3_foundation(
    nl: str,
    matrix: dict[str, Any],
    catalog: dict[str, Any],
    cat_index: dict[tuple[str, str], dict[str, Any]],
    llm: OpenAI,
    model: str,
) -> dict[str, Any]:
    foundation_apps = _candidate_apps_for_cap(
        FOUNDATION_CAP, matrix, cat_index
    )
    if not foundation_apps:
        raise SystemExit(
            f"no app offers {FOUNDATION_CAP!r} in the matrix; cannot fall back"
        )
    by_id = {a["app_id"]: a for a in catalog["apps"]}
    options = [
        {
            "app_id": app_id,
            "app_name": by_id.get(app_id, {}).get("app_name", app_id),
            "locale": by_id.get(app_id, {}).get("locale") or [],
            "locale_policy": locale_policy_text(first_locale(by_id.get(app_id))),
            "agent_name": by_id.get(app_id, {}).get("agent_name"),
            "agent_description": by_id.get(app_id, {}).get("agent_description", ""),
        }
        for app_id in foundation_apps
    ]
    user = (
        "General-assistant app options:\n"
        f"{json.dumps(options, ensure_ascii=False, indent=2)}\n\n"
        f"User request:\n{nl}\n\n"
        "Return the routing JSON now."
    )
    data = _llm_json(llm, model, _STAGE3_SYSTEM, user)
    if data.get("kind") != "app":
        raise RuntimeError(f"stage-3 returned unsupported kind: {data!r}")
    data["capability_id"] = FOUNDATION_CAP
    if data.get("app_id") not in foundation_apps:
        raise SystemExit(
            f"stage-3 picked app_id={data.get('app_id')!r} without {FOUNDATION_CAP}"
        )
    logger.info(
        f"stage-3 foundation -> {data['app_id']} (reason: {data.get('reason')})"
    )
    return data


def route(
    nl: str,
    catalog: dict[str, Any],
    matrix: dict[str, Any],
    llm: OpenAI,
    model: str,
    *,
    preserve_goal: bool = False,
) -> dict[str, Any]:
    """Route one natural-language task to one app/capability.

    `preserve_goal` lets flow planning use the router only for app/capability
    selection while keeping the planner's templated prompt intact.
    """
    cat_index = _catalog_index(catalog)

    cap_ids = _stage1_prefilter(nl, matrix, llm, model)
    if cap_ids:
        decision = _stage2_rerank(
            nl,
            cap_ids,
            matrix,
            cat_index,
            llm,
            model,
        )
        if decision is not None:
            if preserve_goal:
                decision["goal"] = nl
            return decision

    logger.info("falling back to foundation_llm (stage-3)")
    decision = _stage3_foundation(
        nl, matrix, catalog, cat_index, llm, model
    )
    if preserve_goal:
        decision["goal"] = nl
    return decision
