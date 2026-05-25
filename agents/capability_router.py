"""LLM-based capability matcher.

Takes the user's instruction and one app's card, asks the same VLM/LLM
that MobileWorld is driving (via the agent's openai client) to pick a
capability id and render an invocation prompt for the in-app agent.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from loguru import logger

_FORCE_CAP_ENV = "APPCARDS_FORCE_CAPABILITY"
_FORCE_INVOCATION_ENV = "APPCARDS_INVOCATION_TEXT"

_SYSTEM = """You route a user instruction to one in-app AI agent capability.

You are given:
- The user's natural-language instruction.
- One app's capability list (each entry has id, description, example_prompts,
  side_effects, handoff_to_user_required).

Pick exactly one capability id that best matches the instruction, and write
the text the OS-level agent should type into the in-app agent's input box.
Use the user's own wording when possible; expand only to fill obvious gaps.

Reply with ONE JSON object inside a ```json``` fence, with exactly these keys:
{
  "capability_id": "<id from the list>",
  "invocation_text": "<the prompt to type into the in-app agent>",
  "reason": "<one short sentence>"
}
"""


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    m = _FENCE_RE.search(text)
    payload = m.group(1) if m else text
    return json.loads(payload)


def _capability_digest(card: dict) -> list[dict]:
    out = []
    for cap in card["embedded_agent"]["capabilities"]:
        out.append({
            "id": cap["id"],
            "description": cap["description"].strip(),
            "example_prompts": cap.get("example_prompts", []),
            "side_effects": cap.get("side_effects", []),
            "handoff_to_user_required": cap.get("handoff_to_user_required", False),
            "executable": cap.get("executable", True),
        })
    return out


def route_capability(
    agent,
    instruction: str,
    card: dict,
) -> tuple[str, str]:
    """Run one chat completion via the agent's openai client.

    Returns (capability_id, invocation_text). Raises if the LLM reply is
    unparseable or names an unknown capability.

    If APPCARDS_FORCE_CAPABILITY is set (used by the flow runner to skip
    routing in single-capability sub-runs), it is validated against the
    card and returned as-is, paired with APPCARDS_INVOCATION_TEXT (falling
    back to the original instruction).
    """
    forced = os.getenv(_FORCE_CAP_ENV)
    if forced:
        known = {c["id"] for c in card["embedded_agent"]["capabilities"]}
        if forced not in known:
            raise ValueError(
                f"{_FORCE_CAP_ENV}={forced!r} not in card capabilities {sorted(known)}"
            )
        invocation = os.getenv(_FORCE_INVOCATION_ENV) or instruction
        logger.info(f"Capability router skipped (forced): {forced!r}")
        return forced, invocation.strip()

    user_msg = json.dumps(
        {
            "instruction": instruction,
            "app_id": card["app_id"],
            "app_name": card["app_name"],
            "embedded_agent": card["embedded_agent"]["name"],
            "capabilities": _capability_digest(card),
        },
        ensure_ascii=False,
        indent=2,
    )

    raw = agent.openai_chat_completions_create(
        model=agent.model_name,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.0,
        max_tokens=512,
    )
    if not raw:
        raise RuntimeError("Capability router: empty LLM response")

    logger.debug(f"Capability router raw reply: {raw}")
    data = _extract_json(raw)
    cap_id = data["capability_id"]
    invocation = data["invocation_text"].strip()

    known = {c["id"] for c in card["embedded_agent"]["capabilities"]}
    if cap_id not in known:
        raise ValueError(f"Capability router picked unknown id {cap_id!r}; known={sorted(known)}")

    logger.info(f"Capability router: {cap_id} — {data.get('reason', '')}")
    return cap_id, invocation
