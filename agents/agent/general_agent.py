"""Manifest-free general GUI fallback agent (the `general` flow leg).

When a request (or one leg of it) falls outside every card's coverage and no
MobileWorld runtime is available — the on-device app, or a host without the
`mw` extra — the flow hands the leg to this agent instead of failing (see
flow_planner_mw / flow_runner._run_general_step). It re-drives the UI step by
step from the accessibility tree, exactly like the A11yTextAgent baseline it
subclasses, with three differences:

- **System prompt**: free-form GUI driving (open apps, navigate screens, read
  results) rather than the baseline's "use the in-app AI assistant" framing —
  fallback legs are precisely the ones with no in-app assistant to lean on.
- **Final answer**: on information tasks the model is asked to put the found
  text in `finish.answer`; the inherited finish handler persists it through
  RELAY_REPLY_OUT / agent_reply.json so the flow's bind/extract can consume it.
- **HOME start**: when the leg carries no app hint, RELAY_TARGET_APP is the
  `__home__` sentinel — instead of cold-launching, the agent presses HOME and
  finds the right app itself (same contract as MW's general_e2e).

Safety parity with the baseline: a tap landing on an irreversible CTA label is
converted to an ask_user handoff — the fallback must never pay/submit on its
own (see _CTA_LABELS in a11y_agent).

Run standalone via:
    RELAY_AGENT_FILE=$PWD/agents/agent/general_agent.py \
        python -m agents.runtime.native_runner <pkg-or-__home__> "<goal>"
"""

from __future__ import annotations

import time

from loguru import logger

# Alias ordering: _load_agent_class picks the alphabetically FIRST BaseAgent
# subclass bound in this module — the underscore prefix sorts the base class
# after GeneralGUIAgent (same trick as relay_agent's _MCPAgentBase).
from agents.agent.a11y_agent import A11yTextAgent as _A11yTextAgentBase
from agents.device import Key, get_backend
from agents.flow.flow_runner_util import GENERAL_HOME_TARGET

_SYSTEM = (
    "You operate an Android phone to fulfill the user's request by driving the "
    "GUI one step at a time: open the right app, navigate its screens, fill in "
    "fields, and read results. You may start from the HOME screen — open an app "
    "by tapping its icon (scroll or open the app drawer if it is not visible). "
    "You are given the on-screen interactable elements as a numbered "
    "accessibility-tree listing (NOT a screenshot).\n\n"
    "SAFETY: never actually pay, transfer money, or submit an order/booking. "
    "When you reach a payment / final-confirm screen, STOP and reply with "
    "finish(complete).\n\n"
    "Reply with EXACTLY ONE action as a single JSON object, no prose:\n"
    '  {"action":"tap","index":<i>}            tap element i\n'
    '  {"action":"input","text":"<s>"}          type s into the focused field '
    "(tap the input box first on a previous step)\n"
    '  {"action":"scroll","direction":"down"}   scroll down|up to reveal more\n'
    '  {"action":"wait"}                         wait for the screen to settle '
    "or content to load\n"
    '  {"action":"finish","status":"complete","answer":"<found text>"}  task '
    "done — when the request asks for information, ALWAYS put the answer text "
    "you found on screen in `answer` (omit it for pure do-tasks; use status "
    "incomplete if stuck)"
)


class GeneralGUIAgent(_A11yTextAgentBase):
    """Manifest-free general GUI driver for fallback legs."""

    SYSTEM_PROMPT = _SYSTEM
    # Wider irreversible-CTA stop-list than the baseline: fallback legs drive
    # arbitrary apps (often non-Chinese), so cover English checkout/booking
    # wording and money transfer too. A tap landing on any of these becomes an
    # ask_user handoff (inherited behavior) — the fallback never pays, orders,
    # books or transfers on its own. Task-agnostic labels only.
    CTA_LABELS = _A11yTextAgentBase.CTA_LABELS + (
        "确认转账", "立即转账", "确认付款", "确认购买", "提交订单并支付",
        "Pay now", "Place order", "Confirm payment", "Confirm purchase",
        "Buy now", "Book now", "Confirm booking", "Complete purchase",
        "Submit order", "Checkout",
    )

    def _begin_task_once(self) -> None:
        # HOME-start legs (no app hint): suppress the inherited cold-launch —
        # blanking target_app for the super() call keeps its wall-clock /
        # recorder anchoring — then go HOME so the agent starts from a known
        # screen and picks the app itself.
        if self.target_app != GENERAL_HOME_TARGET:
            super()._begin_task_once()
            return
        if self._task_started:
            return
        saved = self.target_app
        self.target_app = ""
        try:
            super()._begin_task_once()
        finally:
            self.target_app = saved
        logger.info("general fallback: no app hint — starting from HOME")
        try:
            get_backend().key(Key.HOME)
            # Same settle a cold_launch gets, so the first a11y dump reads the
            # launcher rather than the previous foreground app.
            time.sleep(1.0)
        except Exception as e:  # HOME is best-effort; the agent can still act
            logger.warning(f"general fallback: HOME press failed: {e}")
