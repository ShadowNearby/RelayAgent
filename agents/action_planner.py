"""Compile a card + chosen capability into an ordered list of logical steps.

Steps are deliberately semantic, not raw JSONActions, because text-based
selectors need runtime visual grounding from the current screenshot. The
adapter (`appcards_agent.py`) walks this list and turns each step into a
MobileWorld JSONAction at predict() time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Step:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    note: str = ""


def _compile_step(raw: dict) -> Step | None:
    """One card step → one logical Step. None if unsupported."""
    if "tap" in raw:
        return _compile_selector_tap(raw["tap"])
    if "tap_label" in raw:
        return Step("tap_text", {"text": raw["tap_label"]["text_or_desc"]})
    if "tap_screen_fraction" in raw:
        f = raw["tap_screen_fraction"]
        return Step("tap_fraction", {"x_ratio": f["x_ratio"], "y_ratio": f["y_ratio"]})
    if "wait" in raw:
        w = raw["wait"]
        if "ms" in w:
            return Step("wait_ms", {"ms": int(w["ms"])})
        if "until" in w:
            sel = w["until"]
            text = sel.get("text") or sel.get("text_contains")
            timeout = int(w.get("timeout_seconds", 5) * 1000)
            return Step("wait_text", {"text": text, "timeout_ms": timeout})
        return Step("wait_ms", {"ms": 1000})
    if "swipe" in raw:
        return Step("swipe", {"direction": raw["swipe"]})
    return None


def _compile_selector_tap(sel: dict) -> Step:
    if "x_bounds" in sel:
        return Step("tap_bounds", {"bounds": sel["x_bounds"]})
    for key in ("text", "text_contains", "accessibility_id", "resource_id"):
        if key in sel:
            return Step("tap_text", {"text": sel[key]})
    return Step("unsupported", {"selector": sel})


def build_plan(
    card: dict,
    capability_id: str,
    invocation_text: str,
    *,
    fresh_conversation: bool = True,
) -> list[Step]:
    ea = card["embedded_agent"]
    capability = next(
        c for c in ea["capabilities"] if c["id"] == capability_id
    )

    plan: list[Step] = []

    plan.append(Step("open_app", {"package": card["app_id"]}, note="cold-launch"))
    plan.append(Step("wait_ms", {"ms": 2500}, note="cold-launch settle"))

    # Optional: start a fresh conversation so prior context does not bleed in.
    # Cards may declare the prep flow under embedded_agent.entry.x_prepare_fresh_conversation.
    if fresh_conversation:
        fresh_steps = (
            (ea.get("entry") or {})
            .get("x_prepare_fresh_conversation", {})
            .get("steps", [])
            or []
        )
        for raw in fresh_steps:
            s = _compile_step(raw)
            if s is not None:
                s.note = s.note or "fresh conversation"
                plan.append(s)

    for raw in ea["entry"]["primary"].get("steps", []) or []:
        s = _compile_step(raw)
        if s is not None:
            plan.append(s)

    field_sel = ea["invocation"]["input"]["field"]
    focus = _compile_selector_tap(field_sel)
    focus.note = "focus input"
    plan.append(focus)

    plan.append(Step("input_text", {"text": invocation_text}, note="user query"))

    submit_sel = ea["invocation"]["submit"]["trigger"]
    submit = _compile_selector_tap(submit_sel)
    submit.note = "submit"
    plan.append(submit)

    # Wait for the in-app agent to finish responding. We give it a generous
    # ceiling (3× typical latency, min 30 s) and let the VLM poll decide when
    # the reply is actually done — and capture the text while we are there.
    typical_latency = capability.get("typical_latency_seconds", 10)
    max_wait = max(int(typical_latency * 3), 30)
    plan.append(
        Step(
            "wait_for_reply",
            {"max_seconds": max_wait, "poll_interval_seconds": 2},
            note="agent reply (VLM-polled)",
        )
    )

    for raw in (capability.get("x_post_result_flow") or {}).get("steps", []) or []:
        s = _compile_step(raw)
        if s is not None:
            plan.append(s)

    if capability.get("handoff_to_user_required", False):
        plan.append(Step("handoff", {"reason": "card declares handoff_to_user_required"}))
    else:
        plan.append(Step("done", {"status": "complete"}))

    return plan
