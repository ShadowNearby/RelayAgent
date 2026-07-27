"""Pin the card→plan compiler (agents/agent/action_planner.py) — previously
zero-covered. Focus points from CLAUDE.md's adapter/manifest conventions:

- manifest `swipe: <direction>` compiles to a logical Step("swipe", direction)
  and _materialize emits a `scroll` action with the SAME direction (the
  finger-gesture reversal lives in NativeEnv._dispatch, pinned here too so the
  whole chain "card swipe → scroll action → reversed gesture" is covered);
- fresh_conversation / skip_open_app plan-head insertion;
- focus→input_text→submit sequence, wait_for_reply 5×/60s formula,
  x_skip_wait_for_reply settle branch, x_capture_full_reply opt-in,
  copy_button tail step, handoff vs done terminals;
- tap_label selector priority and the _compile_step step-kind table.

These test CURRENT behavior (including the bare StopIteration on an unknown
capability id).
"""
from __future__ import annotations

import unittest
from unittest import mock

from agents.agent.action_planner import (
    Step,
    _compile_step,
    _wait_for_reply_step,
    build_plan,
)


def _card(
    capability: dict | None = None,
    *,
    output: dict | None = None,
    fresh_steps: list | None = None,
    entry_steps: list | None = None,
) -> dict:
    cap = {"id": "cap", "typical_latency_seconds": 10}
    cap.update(capability or {})
    ea: dict = {
        "name": "小例",
        "entry": {"primary": {"method": "tap_sequence", "steps": entry_steps or []}},
        "invocation": {
            "input": {"field": {"text": "发消息"}},
            "submit": {"trigger": {"screen_fraction": {"x_ratio": 0.9, "y_ratio": 0.88}}},
        },
        "capabilities": [cap],
    }
    if fresh_steps is not None:
        ea["entry"]["x_prepare_fresh_conversation"] = {"steps": fresh_steps}
    if output is not None:
        ea["output"] = output
    return {"app_id": "com.example.app", "app_name": "Example", "embedded_agent": ea}


def _kinds(plan: list[Step]) -> list[str]:
    return [s.kind for s in plan]


class BuildPlanShapeTests(unittest.TestCase):
    def test_default_plan_open_app_and_settle(self):
        plan = build_plan(_card(), "cap", "hi", fresh_conversation=False, skip_open_app=False)
        self.assertEqual(
            _kinds(plan),
            ["open_app", "wait_ms", "tap_text", "input_text", "tap_fraction",
             "wait_for_reply", "done"],
        )
        self.assertEqual(plan[0].payload, {"package": "com.example.app"})
        self.assertEqual(plan[1].payload, {"ms": 1000})  # cold-launch settle
        self.assertEqual(plan[2].payload, {"text": "发消息"})
        self.assertEqual(plan[2].note, "focus input")
        self.assertEqual(plan[3].payload, {"text": "hi"})
        self.assertEqual(plan[4].note, "submit")
        self.assertEqual(plan[-1].payload, {"status": "complete"})

    def test_skip_open_app_drops_launch_head(self):
        plan = build_plan(_card(), "cap", "hi", fresh_conversation=False, skip_open_app=True)
        self.assertEqual(plan[0].kind, "tap_text")
        self.assertNotIn("open_app", _kinds(plan))

    def test_fresh_conversation_steps_inserted_with_note(self):
        card = _card(fresh_steps=[{"tap_label": {"text_or_desc": "新建对话"}}])
        plan = build_plan(card, "cap", "hi", fresh_conversation=True, skip_open_app=True)
        self.assertEqual(plan[0].kind, "tap_text")
        self.assertEqual(plan[0].payload, {"text": "新建对话"})
        self.assertEqual(plan[0].note, "fresh conversation")

        plan_off = build_plan(card, "cap", "hi", fresh_conversation=False, skip_open_app=True)
        self.assertEqual(plan_off[0].payload, {"text": "发消息"})  # straight to focus

    def test_entry_primary_and_pre_invocation_order(self):
        card = _card(
            capability={"x_pre_invocation_steps": [{"tap_label": {"text": "AI PPT"}}]},
            entry_steps=[{"tap": {"screen_fraction": {"x_ratio": 0.5, "y_ratio": 0.5}}}],
        )
        plan = build_plan(card, "cap", "hi", fresh_conversation=False, skip_open_app=True)
        # entry primary → pre-invocation → focus
        self.assertEqual(_kinds(plan)[:3], ["tap_fraction", "tap_text", "tap_text"])
        self.assertEqual(plan[1].payload, {"text": "AI PPT"})
        self.assertEqual(plan[1].note, "capability pre-invocation")

    def test_unknown_capability_raises_stopiteration(self):
        # Pin the CURRENT error shape: build_plan uses next() without default.
        with self.assertRaises(StopIteration):
            build_plan(_card(), "nope", "hi")


class WaitForReplyCompileTests(unittest.TestCase):
    def _wait_step(self, capability: dict) -> Step:
        plan = build_plan(_card(capability), "cap", "hi",
                          fresh_conversation=False, skip_open_app=True)
        (step,) = [s for s in plan if s.kind == "wait_for_reply"]
        return step

    def test_ceiling_formula_5x_with_60s_floor(self):
        self.assertEqual(
            self._wait_step({"typical_latency_seconds": 20}).payload["max_seconds"], 100
        )
        self.assertEqual(
            self._wait_step({"typical_latency_seconds": 5}).payload["max_seconds"], 60
        )

    def test_x_max_wait_seconds_override(self):
        self.assertEqual(
            self._wait_step({"x_max_wait_seconds": 42}).payload["max_seconds"], 42
        )

    def test_capture_full_opt_in(self):
        p = self._wait_step({"x_capture_full_reply": True}).payload
        self.assertTrue(p["capture_full"])
        self.assertNotIn("max_capture_scrolls", p)  # runtime default (6)

        p = self._wait_step({"x_capture_full_reply": {"max_scrolls": 15}}).payload
        self.assertTrue(p["capture_full"])
        self.assertEqual(p["max_capture_scrolls"], 15)

    def test_no_capture_by_default(self):
        self.assertNotIn("capture_full", self._wait_step({}).payload)

    def test_x_skip_wait_for_reply_becomes_settle_wait(self):
        plan = build_plan(_card({"x_skip_wait_for_reply": True}), "cap", "hi",
                          fresh_conversation=False, skip_open_app=True)
        self.assertNotIn("wait_for_reply", _kinds(plan))
        settle = plan[-2]
        self.assertEqual(settle.kind, "wait_ms")
        self.assertEqual(settle.payload, {"ms": 1500})
        self.assertEqual(settle.note, "settle before handoff")

    def test_x_skip_wait_settle_ms_override(self):
        plan = build_plan(
            _card({"x_skip_wait_for_reply": True, "x_skip_wait_settle_ms": 800}),
            "cap", "hi", fresh_conversation=False, skip_open_app=True,
        )
        self.assertEqual(plan[-2].payload, {"ms": 800})


class TailStepTests(unittest.TestCase):
    def test_handoff_when_card_requires_it(self):
        plan = build_plan(_card({"handoff_to_user_required": True}), "cap", "hi",
                          fresh_conversation=False, skip_open_app=True)
        self.assertEqual(plan[-1].kind, "handoff")
        self.assertIn("handoff_to_user_required", plan[-1].payload["reason"])

    def test_copy_button_step_after_reply_wait(self):
        card = _card(output={
            "method": "copy_button",
            "x_copy_button": {
                "text": "复制",
                "screen_fraction": {"x_ratio": 0.9, "y_ratio": 0.5},
                "valid_x": [800, 1000],
                "valid_y": [0, 2000],
            },
        })
        plan = build_plan(card, "cap", "hi", fresh_conversation=False, skip_open_app=True)
        kinds = _kinds(plan)
        self.assertEqual(
            kinds[kinds.index("wait_for_reply"):], ["wait_for_reply", "copy_reply", "done"]
        )
        (copy_step,) = [s for s in plan if s.kind == "copy_reply"]
        self.assertEqual(copy_step.payload["text"], "复制")
        self.assertEqual(copy_step.payload["valid_x"], [800, 1000])
        self.assertEqual(copy_step.payload["valid_y"], [0, 2000])
        self.assertIn("screen_fraction", copy_step.payload)

    def test_post_result_flow_steps_appended(self):
        plan = build_plan(
            _card({"x_post_result_flow": {"steps": [{"swipe": "up"}]}}), "cap", "hi",
            fresh_conversation=False, skip_open_app=True,
        )
        kinds = _kinds(plan)
        self.assertEqual(kinds[kinds.index("wait_for_reply"):], ["wait_for_reply", "swipe", "done"])


class CompileStepTests(unittest.TestCase):
    def test_swipe_direction_compiles_verbatim(self):
        # Manifest `swipe: <dir>` is written in SCROLL semantics (content
        # movement), and the compiler must carry the direction through
        # unchanged — the finger-gesture reversal happens later, in
        # NativeEnv._dispatch (see CLAUDE.md 「卡片 swipe → scroll 动作」).
        for d in ("up", "down", "left", "right"):
            s = _compile_step({"swipe": d})
            self.assertEqual((s.kind, s.payload), ("swipe", {"direction": d}))

    def test_tap_selector_forms(self):
        s = _compile_step({"tap": {"screen_fraction": {"x_ratio": 0.2, "y_ratio": 0.4}}})
        self.assertEqual((s.kind, s.payload), ("tap_fraction", {"x_ratio": 0.2, "y_ratio": 0.4}))
        s = _compile_step({"tap": {"text": "确定"}})
        self.assertEqual((s.kind, s.payload), ("tap_text", {"text": "确定"}))
        s = _compile_step({"tap": {"resource_id": "com.x:id/btn"}})
        self.assertEqual((s.kind, s.payload), ("tap_text", {"text": "com.x:id/btn"}))
        s = _compile_step({"tap": {"weird": 1}})
        self.assertEqual(s.kind, "unsupported")

    def test_tap_label_priority_and_empty(self):
        s = _compile_step({"tap_label": {"text": "b", "text_or_desc": "a"}})
        self.assertEqual(s.payload, {"text": "a"})  # text_or_desc outranks text
        self.assertIsNone(_compile_step({"tap_label": {}}))

    def test_wait_forms(self):
        s = _compile_step({"wait": {"ms": 3000}})
        self.assertEqual((s.kind, s.payload), ("wait_ms", {"ms": 3000}))
        s = _compile_step({"wait": {"until": {"text": "完成"}, "timeout_seconds": 0.5}})
        self.assertEqual((s.kind, s.payload), ("wait_text", {"text": "完成", "timeout_ms": 500}))
        # until without text → fixed-timeout fallback
        s = _compile_step({"wait": {"until": {"resource_id": "x"}, "timeout_seconds": 2}})
        self.assertEqual((s.kind, s.payload), ("wait_ms", {"ms": 2000}))
        # bare wait → 1000ms default
        s = _compile_step({"wait": {}})
        self.assertEqual((s.kind, s.payload), ("wait_ms", {"ms": 1000}))

    def test_inline_wait_for_reply(self):
        s = _compile_step({"wait_for_reply": {"max_seconds": 30,
                                              "x_capture_full_reply": {"max_scrolls": 4}}})
        self.assertEqual(s.kind, "wait_for_reply")
        self.assertEqual(s.payload,
                         {"max_seconds": 30, "capture_full": True, "max_capture_scrolls": 4})

    def test_tap_unless_present(self):
        s = _compile_step({"tap_unless_present": {"probe": {"text": "已登录"},
                                                  "target": {"screen_fraction": {"x_ratio": 0.5, "y_ratio": 0.5}}}})
        self.assertEqual(s.kind, "tap_unless_present")

    def test_unknown_step_dropped(self):
        self.assertIsNone(_compile_step({"frobnicate": 1}))

    def test_wait_for_reply_step_helper(self):
        self.assertEqual(_wait_for_reply_step(60).payload, {"max_seconds": 60})
        self.assertEqual(
            _wait_for_reply_step(60, {"max_scrolls": 8}).payload,
            {"max_seconds": 60, "capture_full": True, "max_capture_scrolls": 8},
        )


class SwipeDirectionChainTests(unittest.TestCase):
    """The full direction chain: card `swipe: up` → Step swipe up →
    _materialize scroll(up) → NativeEnv reverses vertical directions into the
    underlying finger gesture (scroll up = finger swipes DOWN)."""

    def test_materialize_emits_scroll_with_same_direction(self):
        from agents.agent.relay_agent import RelayAgent

        agent = RelayAgent.__new__(RelayAgent)  # swipe branch touches no state
        for d in ("up", "down", "left", "right"):
            action, advance, _ = agent._materialize(
                Step("swipe", {"direction": d}), None, 1080, 2400
            )
            self.assertEqual(action.action_type, "scroll")
            self.assertEqual(action.direction, d)
            self.assertTrue(advance)

    def test_native_env_reverses_vertical_scroll(self):
        from agents.agent.action_model import JSONAction
        from agents.runtime.native_runtime import NativeEnv

        backend = mock.Mock()
        backend.screen_size.return_value = (1000, 2000)
        env = NativeEnv(step_wait_time=0, backend=backend)

        env._dispatch(JSONAction(action_type="scroll", direction="up"))
        (x0, y0, x1, y1) = backend.swipe_gesture.call_args[0]
        self.assertGreater(y1, y0)  # content up → finger swipes DOWN

        backend.swipe_gesture.reset_mock()
        env._dispatch(JSONAction(action_type="scroll", direction="down"))
        (x0, y0, x1, y1) = backend.swipe_gesture.call_args[0]
        self.assertLess(y1, y0)  # content down → finger swipes UP

        backend.swipe_gesture.reset_mock()
        env._dispatch(JSONAction(action_type="scroll", direction="left"))
        (x0, y0, x1, y1) = backend.swipe_gesture.call_args[0]
        self.assertLess(x1, x0)  # horizontals are NOT reversed


if __name__ == "__main__":
    unittest.main()
