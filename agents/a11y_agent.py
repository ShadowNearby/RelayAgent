"""a11y-text-input baseline (§8.9 item 2 follow-up).

A pure step-by-step *re-driving* agent that, unlike a pure-VLM general agent
(which is fed a screenshot + a 3-image visual history every step), is fed the
**accessibility tree as text** and nothing else. It isolates a single variable
vs. the pure-VLM baseline — the input *modality* (a11y text vs. screenshots) — while
holding everything else constant: it still re-drives the UI step by step, still
uses the app's in-app assistant, same task / model / gateway / step cap /
cold-launch / "do not actually pay" instruction.

The point it measures: delegation's durable lever is the *number* of LLM
round-trips (RA ≈2 vs. re-driving ≈one-per-UI-step), not the size of each call.
a11y text shrinks the per-call input (one screenshot ≈2783 prompt tokens → a
pruned tree ≈hundreds), but the step count is unchanged, so re-driving still
pays a round-trip per UI step that delegation removes.

It subclasses RelayAgent only to inherit the LLM-call logging wrapper
(`openai_chat_completions_create` → per-call `usage_delta` in traj.json, which
`scripts/aggregate_metrics.py` reads) and `build_openai_client`. It loads NO
card and builds NO plan.

Run via:  RELAY_AGENT_FILE=$PWD/agents/a11y_agent.py python -m agents.native_runner <pkg> "<goal>"
Knobs:    A11Y_MAX_NODES (60) · A11Y_TEXT_TRUNC (50) · A11Y_HISTORY_K (12)
          A11Y_STEP_CAP (50) · A11Y_MAX_DUMP_FAIL (4)
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from loguru import logger

from agents.action_model import JSONAction

from agents.device import UINode, get_backend
from agents.relay_agent import RelayAgent

# Task-agnostic irreversible-action labels. A tap landing on one of these is
# converted to a handoff so the baseline cannot cross a CTA — keeps §8.6 safety
# parity with RA. This list carries no per-app task knowledge.
_CTA_LABELS = (
    "支付宝付款", "微信支付", "立即支付", "确认支付", "去支付", "提交订单",
    "立即下单", "确认下单", "立即打车", "确认叫车", "确认下单并支付", "立即购买",
)

_SYSTEM = (
    "You operate an Android phone to fulfill the user's request by driving the "
    "UI one step at a time. You MAY (and should) use the app's built-in AI "
    "assistant: type the request into its chat box, send it, and follow the "
    "cards/options it returns. You are given the on-screen interactable elements "
    "as a numbered accessibility-tree listing (NOT a screenshot).\n\n"
    "SAFETY: never actually pay or submit an order/ride. When you reach a "
    "payment / final-confirm screen (a 支付/付款/立即打车 button is visible), "
    "STOP and reply with finish(complete).\n\n"
    "Reply with EXACTLY ONE action as a single JSON object, no prose:\n"
    '  {"action":"tap","index":<i>}            tap element i\n'
    '  {"action":"input","text":"<s>"}          type s into the focused field '
    "(tap the input box first on a previous step)\n"
    '  {"action":"scroll","direction":"down"}   scroll down|up to reveal more\n'
    '  {"action":"wait"}                         wait for the assistant to '
    "respond / the screen to settle\n"
    '  {"action":"finish","status":"complete"}  task done or payment screen '
    "reached (or status incomplete if stuck)"
)


def serialize_tree(
    nodes: list[UINode], screen_w: int, screen_h: int, max_nodes: int, trunc: int
) -> tuple[list[dict], str]:
    """Task-agnostic accessibility-tree serialization (WebVoyager / AutoGLM /
    SeeAct style). Keep every node that is interactable (clickable /
    long-clickable / scrollable / editable) OR carries text/content-desc; drop
    pure layout containers. One line per node: `[i] <Role> "label" {flags}`.
    No per-app knowledge — same rule for all apps."""
    out: list[dict] = []
    for n in nodes:
        editable = n.class_name.endswith("EditText") or (
            n.focusable and "Edit" in n.class_name
        )
        label = n.text or n.desc
        if not (n.clickable or n.long_clickable or n.scrollable or editable or label):
            continue
        c = n.center  # None already filters absent/zero-area bounds
        if c is None:
            continue
        cx, cy = c
        if cx < 0 or cy < 0 or cx > screen_w or cy > screen_h:
            continue
        out.append({
            "cx": cx, "cy": cy, "label": label,
            "role": (n.class_name.split(".")[-1] or "View"),
            "editable": bool(editable),
            "scrollable": bool(n.scrollable),
        })

    truncated = len(out) > max_nodes
    out = out[:max_nodes]
    lines: list[str] = []
    for i, nd in enumerate(out):
        flags = []
        if nd["editable"]:
            flags.append("editable")
        if nd["scrollable"]:
            flags.append("scrollable")
        fs = (" {" + ",".join(flags) + "}") if flags else ""
        lines.append(f'[{i}] {nd["role"]} "{nd["label"][:trunc]}"{fs}')
    listing = "\n".join(lines) if lines else "(no interactable elements found)"
    if truncated:
        listing += "\n... (list truncated)"
    return out, listing


def parse_action(raw: str) -> dict:
    """Tolerant parse of the model's one-action JSON. Falls back to wait."""
    if not raw:
        return {"action": "wait"}
    # First try a fenced or bare JSON object containing "action".
    for m in re.finditer(r"\{.*?\}", raw, re.S):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "action" in obj:
            return obj
    # Regex fallbacks for tap(i) / input("..") shorthands.
    mt = re.search(r"\btap\b\D*(\d+)", raw)
    if mt:
        return {"action": "tap", "index": int(mt.group(1))}
    if re.search(r"\bfinish", raw):
        return {"action": "finish", "status": "complete"}
    if re.search(r"\bscroll", raw):
        return {"action": "scroll", "direction": "up" if "up" in raw else "down"}
    return {"action": "wait"}


class A11yTextAgent(RelayAgent):
    """Pure re-driving baseline fed the a11y tree as text (no screenshots)."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.max_nodes = int(os.getenv("A11Y_MAX_NODES", "60"))
        self.text_trunc = int(os.getenv("A11Y_TEXT_TRUNC", "50"))
        self.history_k = int(os.getenv("A11Y_HISTORY_K", "12"))
        self.step_cap = int(os.getenv("A11Y_STEP_CAP", "50"))
        self.max_dump_fail = int(os.getenv("A11Y_MAX_DUMP_FAIL", "4"))
        self._screen: tuple[int, int] | None = None
        self._history: list[str] = []
        self._nstep = 0
        self._dump_fail_streak = 0

    def initialize_hook(self, instruction: str) -> None:
        # No card, no plan — this is a manifest-free, free-form re-driving agent.
        logger.info(f"A11yTextAgent init: instruction={instruction!r}")
        self.card = None
        self._history = []
        self._nstep = 0
        self._dump_fail_streak = 0

    def reset(self) -> None:  # type: ignore[override]
        super().reset()
        self._history = []
        self._nstep = 0
        self._dump_fail_streak = 0
        self._screen = None

    def _screen_size(self) -> tuple[int, int]:
        if self._screen is None:
            try:
                self._screen = get_backend().screen_size()
            except Exception:
                self._screen = (1080, 2340)
        return self._screen

    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        self._nstep += 1
        if self._nstep > self.step_cap:
            return "step cap reached", JSONAction(
                action_type="finished", goal_status="incomplete"
            )

        tree = get_backend().dump_ui_tree()
        if tree is None:
            self._dump_fail_streak += 1
            if self._dump_fail_streak >= self.max_dump_fail:
                return ("a11y dump failed repeatedly; giving up",
                        JSONAction(action_type="finished", goal_status="incomplete"))
            return (f"dump failed ({self._dump_fail_streak}); waiting",
                    JSONAction(action_type="wait"))
        self._dump_fail_streak = 0

        w, h = self._screen_size()
        nodes, listing = serialize_tree(tree, w, h, self.max_nodes, self.text_trunc)

        hist = "\n".join(self._history[-self.history_k:]) or "(none yet)"
        user = (
            f"User request: {self.instruction}\n\n"
            f"Current screen (interactable elements):\n{listing}\n\n"
            f"Action history:\n{hist}\n\n"
            "Your next action (one JSON object):"
        )
        raw = self.openai_chat_completions_create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
        )
        act = parse_action(raw or "")
        kind = str(act.get("action", "wait")).lower()
        thought = f"step {self._nstep}: {kind} {json.dumps(act, ensure_ascii=False)}"

        if kind == "tap":
            idx = act.get("index")
            if isinstance(idx, str) and idx.strip().lstrip("-").isdigit():
                idx = int(idx)
            if not isinstance(idx, int) or not (0 <= idx < len(nodes)):
                self._history.append(f"{self._nstep}: tap(bad idx {idx}) → noop")
                return f"{thought} [bad index]", JSONAction(action_type="wait")
            nd = nodes[idx]
            label = nd["label"]
            # Safety: a tap on an irreversible CTA is converted to a handoff.
            if any(c in label for c in _CTA_LABELS):
                self._history.append(f"{self._nstep}: reached CTA {label!r} → handoff")
                return (f"{thought} [CTA stop: {label!r}]", JSONAction(
                    action_type="ask_user",
                    text=f"Reached the irreversible action ({label}); "
                         "handing control back without crossing it.",
                ))
            self._history.append(f"{self._nstep}: tap [{idx}] {label!r}")
            return thought, JSONAction(action_type="click", x=nd["cx"], y=nd["cy"])

        if kind == "input":
            text = str(act.get("text", ""))
            self._history.append(f"{self._nstep}: input {text!r}")
            return thought, JSONAction(
                action_type="input_text", text=text
            )

        if kind == "scroll":
            direction = "up" if str(act.get("direction", "down")) == "up" else "down"
            self._history.append(f"{self._nstep}: scroll {direction}")
            return thought, JSONAction(action_type="scroll", direction=direction)

        if kind == "finish":
            status = str(act.get("status", "complete"))
            self._history.append(f"{self._nstep}: finish {status}")
            return thought, JSONAction(
                action_type="finished",
                goal_status="incomplete" if status == "incomplete" else "complete",
            )

        # default: wait
        self._history.append(f"{self._nstep}: wait")
        return thought, JSONAction(action_type="wait")
