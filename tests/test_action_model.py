"""Conformance tests for JSONAction.

Written against the original pydantic implementation first, then kept green
across the pure-Python rewrite (Android/Chaquopy can't ship pydantic-core),
so the two implementations are behavior-identical for every consumer:
construction coercions/validations, `model_dump(exclude_none=True)`, and
`__eq__`.
"""
from __future__ import annotations

import unittest

from agents.agent.action_model import (
    ANSWER,
    ASK_USER,
    CLICK,
    ENV_FAIL,
    FINISHED,
    INPUT_TEXT,
    OPEN_APP,
    SCROLL,
    JSONAction,
)


class ConstructionTests(unittest.TestCase):
    def test_minimal_click(self):
        a = JSONAction(action_type="click", x=100, y=200)
        self.assertEqual(a.action_type, CLICK)
        self.assertEqual((a.x, a.y), (100, 200))
        self.assertIsNone(a.text)
        self.assertIsNone(a.action_json)

    def test_all_fields_default_none(self):
        a = JSONAction()
        for f in ("action_type", "index", "x", "y", "text", "direction",
                  "goal_status", "app_name", "keycode", "start_x", "start_y",
                  "end_x", "end_y", "action_name", "action_json"):
            self.assertIsNone(getattr(a, f), f)

    def test_unknown_keys_ignored(self):
        # pydantic v2 default extra="ignore" — the wire format from the LLM can
        # carry stray keys; they must not crash construction.
        a = JSONAction(action_type="wait", bogus_key=1, thought="hi")
        self.assertEqual(a.action_type, "wait")
        self.assertFalse(hasattr(a, "bogus_key"))

    def test_coordinate_rounding(self):
        a = JSONAction(action_type="click", x=10.6, y=20.4)
        self.assertEqual((a.x, a.y), (11, 20))
        self.assertIsInstance(a.x, int)

    def test_index_coercion(self):
        self.assertEqual(JSONAction(action_type="click", index="3").index, 3)
        self.assertEqual(JSONAction(action_type="click", index=5).index, 5)

    def test_text_coercion(self):
        self.assertEqual(JSONAction(action_type=INPUT_TEXT, text=42).text, "42")
        self.assertEqual(JSONAction(action_type=INPUT_TEXT, text="hi").text, "hi")
        self.assertIsNone(JSONAction(action_type="wait").text)

    def test_invalid_action_type_raises(self):
        with self.assertRaises(ValueError):
            JSONAction(action_type="fly")

    def test_invalid_direction_raises(self):
        with self.assertRaises(ValueError):
            JSONAction(action_type=SCROLL, direction="diagonal")
        for d in ("left", "right", "up", "down"):
            self.assertEqual(JSONAction(action_type=SCROLL, direction=d).direction, d)

    def test_invalid_keycode_raises(self):
        with self.assertRaises(ValueError):
            JSONAction(action_type="wait", keycode="ENTER")
        a = JSONAction(action_type="wait", keycode="KEYCODE_ENTER")
        self.assertEqual(a.keycode, "KEYCODE_ENTER")

    def test_invalid_index_raises(self):
        with self.assertRaises(ValueError):
            JSONAction(action_type="click", index="abc")

    def test_index_xor_xy_raises(self):
        with self.assertRaises(ValueError):
            JSONAction(action_type="click", index=1, x=10, y=20)
        with self.assertRaises(ValueError):
            JSONAction(action_type="click", index=1, x=10)

    def test_action_json_passthrough(self):
        payload = {"skip_screenshot": True, "nested": {"k": [1, 2]}}
        a = JSONAction(action_type="wait", action_json=payload)
        self.assertEqual(a.action_json, payload)

    def test_drag_endpoints(self):
        a = JSONAction(action_type="drag", start_x=1, start_y=2, end_x=3, end_y=4)
        self.assertEqual((a.start_x, a.start_y, a.end_x, a.end_y), (1, 2, 3, 4))

    def test_typical_wire_dicts(self):
        # The shapes predict() actually emits across relay_agent / a11y_agent.
        for d in (
            {"action_type": "click", "x": 540, "y": 1200},
            {"action_type": "input_text", "text": "三杯蜜桃四季春"},
            {"action_type": "open_app", "app_name": "千问"},
            {"action_type": "scroll", "direction": "down"},
            {"action_type": "wait"},
            {"action_type": "answer", "text": "done"},
            {"action_type": "finished", "goal_status": "complete"},
            {"action_type": "ask_user", "text": "which one?"},
            {"action_type": "status", "goal_status": "incomplete"},
            {"action_type": "wait", "action_json": {"skip_screenshot": True}},
        ):
            a = JSONAction(**d)
            self.assertEqual(a.action_type, d["action_type"])


class ModelDumpTests(unittest.TestCase):
    def test_exclude_none(self):
        a = JSONAction(action_type="click", x=10, y=20)
        self.assertEqual(
            a.model_dump(exclude_none=True),
            {"action_type": "click", "x": 10, "y": 20},
        )

    def test_full_dump_has_all_fields(self):
        d = JSONAction(action_type="wait").model_dump()
        self.assertEqual(len(d), 15)
        self.assertIn("action_json", d)
        self.assertIsNone(d["x"])

    def test_dump_field_order_is_declaration_order(self):
        d = JSONAction(action_type="click", x=1, y=2, text="t").model_dump(
            exclude_none=True
        )
        self.assertEqual(list(d), ["action_type", "x", "y", "text"])

    def test_dump_preserves_action_json(self):
        a = JSONAction(action_type="wait", action_json={"skip_screenshot": True})
        self.assertEqual(
            a.model_dump(exclude_none=True),
            {"action_type": "wait", "action_json": {"skip_screenshot": True}},
        )


class EqualityTests(unittest.TestCase):
    def test_eq_same(self):
        self.assertEqual(
            JSONAction(action_type="click", x=1, y=2),
            JSONAction(action_type="click", x=1, y=2),
        )

    def test_eq_case_insensitive_text_and_app(self):
        self.assertEqual(
            JSONAction(action_type=OPEN_APP, app_name="QianWen", text="Hi"),
            JSONAction(action_type=OPEN_APP, app_name="qianwen", text="hi"),
        )

    def test_neq_different_coords(self):
        self.assertNotEqual(
            JSONAction(action_type="click", x=1, y=2),
            JSONAction(action_type="click", x=1, y=3),
        )

    def test_neq_non_action(self):
        self.assertNotEqual(JSONAction(action_type="wait"), "wait")

    def test_eq_ignores_action_json(self):
        # _compare_actions never looked at action_json / action_name — keep it so.
        self.assertEqual(
            JSONAction(action_type="wait", action_json={"a": 1}),
            JSONAction(action_type="wait", action_json={"b": 2}),
        )


class ConstantsTests(unittest.TestCase):
    def test_wire_values(self):
        self.assertEqual(CLICK, "click")
        self.assertEqual(ANSWER, "answer")
        self.assertEqual(FINISHED, "finished")
        self.assertEqual(ASK_USER, "ask_user")

    def test_env_fail_is_constructible(self):
        # ENV_FAIL is a terminal type in native_runtime._TERMINAL_TYPES, so an
        # agent must be able to emit it as an action.
        self.assertEqual(JSONAction(action_type=ENV_FAIL).action_type, "error_env")


if __name__ == "__main__":
    unittest.main()
