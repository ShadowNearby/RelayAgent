"""LLM-call logging helpers for RelayAgent.

Sanitize chat-completion messages/kwargs before they land in traj.json (strip
giant base64 image payloads) and label each call site by its system prompt.
Split out of `relay_agent.py`.
"""

from __future__ import annotations

from agents.agent.relay_grounding import _GROUNDING_SYSTEM
from agents.agent.relay_reply import _REPLY_WATCH_SYSTEM


def _sanitize_messages_for_log(messages: list[dict]) -> list[dict]:
    """Strip giant base64 image_url payloads so traj.json stays readable."""
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            parts: list[dict] = []
            for part in content:
                if not isinstance(part, dict):
                    parts.append({"type": "raw", "value": repr(part)[:200]})
                    continue
                if part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    if isinstance(url, str) and url.startswith("data:"):
                        parts.append({"type": "image_url", "image_url": {
                            "url": f"<base64 image, {len(url)} chars>"
                        }})
                    else:
                        parts.append(part)
                else:
                    parts.append(part)
            out.append({**msg, "content": parts})
        else:
            out.append(msg)
    return out


def _sanitize_kwargs_for_log(kwargs: dict) -> dict:
    return {k: v for k, v in kwargs.items()
            if k in ("temperature", "max_tokens", "max_completion_tokens", "stream")}


def _llm_purpose_from_messages(messages: list[dict]) -> str:
    """Best-effort label for a call site (capability-router / grounding /
    reply-watch / other), inferred from the system prompt."""
    if not messages:
        return "unknown"
    sys_msg = next((m for m in messages if m.get("role") == "system"), None)
    sys = (sys_msg or {}).get("content")
    if not isinstance(sys, str):
        return "unknown"
    if sys.startswith(_GROUNDING_SYSTEM[:40]):
        return "grounding"
    if sys.startswith(_REPLY_WATCH_SYSTEM[:40]):
        return "reply_watch"
    if "capability id" in sys:
        return "capability_router"
    return "other"
