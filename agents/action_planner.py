"""Compile a card + chosen capability into an ordered list of logical steps.

Steps are deliberately semantic, not raw JSONActions, because text-based
selectors need runtime visual grounding from the current screenshot. The
adapter (`appcards_agent.py`) walks this list and turns each step into a
MobileWorld JSONAction at predict() time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class Step:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    note: str = ""


# tap_label selector field priority (matches SPEC §6.1 and the real-device
# test runner in tests/test_manifest_real_adb.py).
_TAP_LABEL_KEYS = (
    "text_or_desc",
    "text",
    "text_or_desc_contains",
    "text_contains",
    "accessibility_id",
)


def _compile_step(raw: dict) -> Step | None:
    """One card step → one logical Step. Returns None on unrecognized step
    (and logs a warning — a silent skip masks card typos)."""
    if "tap" in raw:
        return _compile_selector_tap(raw["tap"])
    if "tap_label" in raw:
        body = raw["tap_label"] or {}
        for key in _TAP_LABEL_KEYS:
            v = body.get(key)
            if v:
                return Step("tap_text", {"text": v})
        logger.warning(f"tap_label step has no usable selector: {body!r}")
        return None
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
            timeout = int(float(w.get("timeout_seconds", 5)) * 1000)
            if not text:
                logger.warning(
                    f"wait.until selector lacks text/text_contains; falling "
                    f"back to fixed {timeout}ms wait: {sel!r}"
                )
                return Step("wait_ms", {"ms": timeout})
            return Step("wait_text", {"text": text, "timeout_ms": timeout})
        return Step("wait_ms", {"ms": 1000})
    if "swipe" in raw:
        return Step("swipe", {"direction": raw["swipe"]})
    if "tap_unless_present" in raw:
        body = raw["tap_unless_present"]
        return Step(
            "tap_unless_present",
            {"probe": body["probe"], "target": body["target"]},
            note="conditional tap (skip if probe present)",
        )
    if "wait_for_reply" in raw:
        w = raw["wait_for_reply"] or {}
        return Step(
            "wait_for_reply",
            {"max_seconds": int(w.get("max_seconds", 60))},
            note="agent reply (VLM-polled)",
        )
    logger.warning(
        f"Unknown step kind in card (no handler matched): {list(raw.keys())!r} "
        f"— step will be dropped from the plan"
    )
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
    skip_open_app: bool = False,
) -> list[Step]:
    ea = card["embedded_agent"]
    capability = next(
        c for c in ea["capabilities"] if c["id"] == capability_id
    )

    plan: list[Step] = []

    # Caller (e.g. scripts/run_test.py) may already have cold-launched
    # the app before invoking MobileWorld. In that case we skip the
    # redundant open_app + settle wait at the top of the plan.
    if not skip_open_app:
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

    # Some capabilities require switching the chat surface into a dedicated
    # sub-mode before typing — e.g. WPS AI's "AI PPT" chip locks the input
    # into PPT-topic-only mode. Capability can declare these prefix steps
    # under `x_pre_invocation_steps`; they run after entry, before the
    # input is focused.
    for raw in capability.get("x_pre_invocation_steps") or []:
        s = _compile_step(raw)
        if s is not None:
            s.note = s.note or "capability pre-invocation"
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
    # Long-form replies (stacked POI cards, multi-paragraph summaries) often
    # exceed one viewport — capability can opt in to a post-done scroll-and-
    # capture loop via `x_capture_full_reply: { max_scrolls: N }` (or just
    # `true` for the default of 6).
    capture_cfg = capability.get("x_capture_full_reply")
    wait_params: dict[str, Any] = {"max_seconds": max_wait}
    if capture_cfg:
        wait_params["capture_full"] = True
        if isinstance(capture_cfg, dict) and "max_scrolls" in capture_cfg:
            wait_params["max_capture_scrolls"] = int(capture_cfg["max_scrolls"])
    plan.append(
        Step(
            "wait_for_reply",
            wait_params,
            note="agent reply (VLM-polled)",
        )
    )

    # If the card declares output.method == copy_button, tap the in-app
    # copy button after the reply lands so the answer ends up on the device
    # clipboard. Reading it back is out of scope (Binder 1MB cap on rich
    # copies); the persisted text in agent_reply.json still comes from VLM.
    # Locator: prefer VLM grounding via `text`, fall back to fixed `x_bounds`.
    output_cfg = ea.get("output") or {}
    if output_cfg.get("method") == "copy_button":
        cfg = output_cfg.get("x_copy_button") or {}
        payload: dict[str, Any] = {}
        if cfg.get("text"):
            payload["text"] = cfg["text"]
        if cfg.get("x_bounds"):
            payload["bounds"] = cfg["x_bounds"]
        if cfg.get("valid_x"):
            payload["valid_x"] = list(cfg["valid_x"])
        if cfg.get("valid_y"):
            payload["valid_y"] = list(cfg["valid_y"])
        if payload:
            plan.append(
                Step(
                    "copy_reply",
                    payload,
                    note="tap in-app copy button (leaves answer on clipboard)",
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
