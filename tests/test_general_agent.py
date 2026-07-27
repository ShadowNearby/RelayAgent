"""Pin the general-fallback agent's contracts (agents/agent/general_agent.py):
the loader picks GeneralGUIAgent (alias-ordering trap), finish(answer=...)
persists the reply for the flow's bind/extract, and a HOME-sentinel leg starts
from the launcher instead of cold-launching.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from agents.agent.general_agent import GeneralGUIAgent
from agents.device import Key
from agents.flow.flow_runner_util import GENERAL_HOME_TARGET

REPO_ROOT = Path(__file__).resolve().parent.parent


def _bare_agent(target_app: str, cls: type = GeneralGUIAgent):
    """An agent (GeneralGUIAgent by default) with only the state
    predict/_begin_task_once touch — no LLM client, no device, no card
    machinery."""
    agent = cls.__new__(cls)
    agent.max_nodes = 60
    agent.text_trunc = 50
    agent.history_k = 12
    agent.step_cap = 50
    agent.max_dump_fail = 4
    agent._screen = (1080, 2400)
    agent._history = []
    agent._nstep = 0
    agent._dump_fail_streak = 0
    agent._last_agent_reply = None
    agent.instruction = "find something"
    agent.model_name = "test-model"
    agent.target_app = target_app
    agent.record_dir = None
    agent.agent_launch = True
    agent._task_started = False
    agent._task_t0 = None
    # Never let the real finalizer (atexit-registered by _begin_task_once)
    # write wall_clock.json into the live traj dir from a unit test.
    agent._finalize_task = lambda: None
    return agent


class LoaderPicksGeneralAgentTests(unittest.TestCase):
    def test_load_agent_class_returns_general_gui_agent(self) -> None:
        # _load_agent_class picks the alphabetically FIRST BaseAgent subclass
        # bound in the module — the underscore alias for the imported base
        # class must keep GeneralGUIAgent first.
        from agents.runtime.native_runner import _load_agent_class

        cls = _load_agent_class(REPO_ROOT / "agents" / "agent" / "general_agent.py")
        self.assertEqual(cls.__name__, "GeneralGUIAgent")


class FinishAnswerTests(unittest.TestCase):
    def _predict(self, agent: GeneralGUIAgent, raw: str):
        agent._task_started = True  # skip launch/anchoring
        persisted: list[str | None] = []
        agent._maybe_persist_reply = lambda: persisted.append(agent._last_agent_reply)
        agent.openai_chat_completions_create = lambda **kw: raw
        with mock.patch("agents.agent.a11y_agent.get_backend") as gb:
            gb.return_value.dump_ui_tree.return_value = []
            thought, action = agent.predict({"screenshot": None})
        return action, persisted

    def test_finish_with_answer_persists_reply(self) -> None:
        agent = _bare_agent("com.example.app")
        action, persisted = self._predict(
            agent,
            '{"action":"finish","status":"complete","answer":"营业到 22:00"}',
        )
        self.assertEqual(action.action_type, "finished")
        self.assertEqual(action.goal_status, "complete")
        self.assertEqual(persisted, ["营业到 22:00"])

    def test_finish_without_answer_does_not_persist(self) -> None:
        agent = _bare_agent("com.example.app")
        action, persisted = self._predict(
            agent, '{"action":"finish","status":"complete"}'
        )
        self.assertEqual(action.action_type, "finished")
        self.assertEqual(persisted, [])


class CtaGuardTests(unittest.TestCase):
    def test_baseline_cta_list_unchanged(self) -> None:
        # Benchmark parity: widening the general agent's stop-list must not
        # touch the a11y baseline's.
        from agents.agent.a11y_agent import _CTA_LABELS, A11yTextAgent

        self.assertEqual(A11yTextAgent.CTA_LABELS, _CTA_LABELS)

    def test_general_agent_widens_cta_list(self) -> None:
        from agents.agent.a11y_agent import _CTA_LABELS

        for label in _CTA_LABELS + ("Pay now", "Confirm booking", "确认转账"):
            self.assertIn(label, GeneralGUIAgent.CTA_LABELS)

    def test_tap_on_cta_becomes_handoff(self) -> None:
        agent = _bare_agent("com.example.app")
        agent._task_started = True
        agent.openai_chat_completions_create = lambda **kw: '{"action":"tap","index":0}'
        from agents.device import UINode

        node = UINode(text="Pay now", desc="", resource_id="", class_name="android.widget.Button",
                      package="p", bounds=(0, 0, 200, 100), clickable=True,
                      focusable=False, enabled=True, scrollable=False, long_clickable=False)
        with mock.patch("agents.agent.a11y_agent.get_backend") as gb:
            gb.return_value.dump_ui_tree.return_value = [node]
            _, action = agent.predict({"screenshot": None})
        self.assertEqual(action.action_type, "ask_user")


class CtaContainerGuardTests(unittest.TestCase):
    """The CTA guard must also catch the common 中文 App pay-button structure:
    a clickable, label-less ViewGroup wrapping a non-clickable CTA TextView.
    The container's own label is empty, so the label-only check never fires —
    the geometric fallback (bounds intersection with any CTA text node) must.
    Both the baseline (A11yTextAgent) and the production fallback
    (GeneralGUIAgent) inherit the same guard."""

    @staticmethod
    def _nodes(inner_text: str, inner_bounds=(340, 1050, 740, 1150)):
        from agents.device import UINode

        container = UINode(
            text="", desc="", resource_id="", class_name="android.view.ViewGroup",
            package="p", bounds=(0, 1000, 1080, 1200), clickable=True,
            focusable=False, enabled=True, scrollable=False, long_clickable=False,
        )
        inner = UINode(
            text=inner_text, desc="", resource_id="",
            class_name="android.widget.TextView", package="p",
            bounds=inner_bounds, clickable=False, focusable=False,
            enabled=True, scrollable=False, long_clickable=False,
        )
        return [container, inner]

    def _tap_container(self, cls, inner_text, inner_bounds=(340, 1050, 740, 1150)):
        agent = _bare_agent("com.example.app", cls=cls)
        agent._task_started = True
        # The serialized listing keeps the container at index 0 (clickable)
        # and the CTA text node at index 1 (has label); the model taps the
        # container.
        agent.openai_chat_completions_create = lambda **kw: '{"action":"tap","index":0}'
        with mock.patch("agents.agent.a11y_agent.get_backend") as gb:
            gb.return_value.dump_ui_tree.return_value = self._nodes(
                inner_text, inner_bounds
            )
            _, action = agent.predict({"screenshot": None})
        return action

    def test_general_agent_stops_on_container_over_cta_text(self) -> None:
        action = self._tap_container(GeneralGUIAgent, "Pay now")
        self.assertEqual(action.action_type, "ask_user")
        self.assertIn("Pay now", action.text)

    def test_baseline_stops_on_container_over_cta_text(self) -> None:
        from agents.agent.a11y_agent import A11yTextAgent

        action = self._tap_container(A11yTextAgent, "立即支付")
        self.assertEqual(action.action_type, "ask_user")
        self.assertIn("立即支付", action.text)

    def test_container_over_benign_text_still_taps(self) -> None:
        action = self._tap_container(GeneralGUIAgent, "查看详情")
        self.assertEqual(action.action_type, "click")
        self.assertEqual((action.x, action.y), (540, 1100))  # container center

    def test_disjoint_cta_text_does_not_trip_guard(self) -> None:
        # CTA text elsewhere on screen (not intersecting the tapped bounds)
        # must not convert an unrelated container tap into a handoff.
        action = self._tap_container(
            GeneralGUIAgent, "Pay now", inner_bounds=(0, 2000, 400, 2100)
        )
        self.assertEqual(action.action_type, "click")

    def test_labeled_cta_node_still_stops(self) -> None:
        # The original label-only tier is untouched: tapping a node whose own
        # label is a CTA word still hands off.
        agent = _bare_agent("com.example.app")
        agent._task_started = True
        agent.openai_chat_completions_create = lambda **kw: '{"action":"tap","index":1}'
        with mock.patch("agents.agent.a11y_agent.get_backend") as gb:
            gb.return_value.dump_ui_tree.return_value = self._nodes("Buy now")
            _, action = agent.predict({"screenshot": None})
        self.assertEqual(action.action_type, "ask_user")


class HomeStartTests(unittest.TestCase):
    def test_home_sentinel_presses_home_instead_of_cold_launch(self) -> None:
        agent = _bare_agent(GENERAL_HOME_TARGET)
        backend = mock.Mock()
        with mock.patch("agents.agent.general_agent.get_backend", return_value=backend), \
             mock.patch("agents.agent.general_agent.time.sleep"), \
             mock.patch("agents.agent.relay_agent._cold_launch") as cold:
            agent._begin_task_once()
        backend.key.assert_called_once_with(Key.HOME)
        cold.assert_not_called()
        self.assertTrue(agent._task_started)
        # target_app restored after the suppressed-launch super() call
        self.assertEqual(agent.target_app, GENERAL_HOME_TARGET)

    def test_app_hint_keeps_inherited_cold_launch(self) -> None:
        agent = _bare_agent("com.example.app")
        with mock.patch("agents.agent.relay_agent._cold_launch") as cold:
            agent._begin_task_once()
        cold.assert_called_once_with("com.example.app")


if __name__ == "__main__":
    unittest.main()
