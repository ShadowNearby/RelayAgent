"""Unit tests for the capability `prompt_template` pipeline.

Two layers, both pure and both contract-bearing (see docs/prompt_template.zh.md):

- fill time (`flow_planner_util._fill_template` / `_fill_slots` and
  `FlowPlanner._fill_prompt_template`): declared slots are substituted,
  `[ ... {slot} ... ]` optional segments are kept or dropped whole, a missing
  REQUIRED slot is a hard failure, and a filled prompt may not reference a
  `{var}` no upstream step bound;
- load time (`card_catalog._validate_prompt_template`): authoring mistakes
  (undeclared placeholder, dead slot, a required slot only inside `[...]`, an
  optional slot outside one, unbalanced/nested brackets) fail the catalog build.

These pin CURRENT behavior. validate_manifests.py in CI only proves the shipped
manifests are legal — it cannot catch the validator itself going lax.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from agents.flow.flow_planner import FlowPlanner, PromptTemplateError
from agents.flow.flow_planner_util import _fill_slots, _fill_template, _has_slot_value
from agents.routing.card_catalog import _validate_prompt_template


# --------------------------------------------------------------------------- #
# _fill_template / _fill_slots (pure)
# --------------------------------------------------------------------------- #


class FillTemplateTests(unittest.TestCase):
    NAV = "Navigate to {place}[ by {mode}]."
    NAMES = ["place", "mode"]

    def test_required_only_drops_optional_segment(self) -> None:
        # Byte-identical to the pre-optional-segment template (docs §2).
        self.assertEqual(
            _fill_template(self.NAV, {"place": "Hong Kong International Airport"}, self.NAMES),
            "Navigate to Hong Kong International Airport.",
        )

    def test_optional_segment_kept_when_filled(self) -> None:
        self.assertEqual(
            _fill_template(self.NAV, {"place": "HKIA", "mode": "driving"}, self.NAMES),
            "Navigate to HKIA by driving.",
        )

    def test_blank_optional_value_drops_the_segment(self) -> None:
        for blank in (None, "", "   "):
            with self.subTest(blank=blank):
                self.assertEqual(
                    _fill_template(self.NAV, {"place": "HKIA", "mode": blank}, self.NAMES),
                    "Navigate to HKIA.",
                )

    def test_segment_is_dropped_with_its_surrounding_wording(self) -> None:
        out = _fill_template(
            "订{n}张[从{origin}]到{dest}的票", {"n": "2", "dest": "北京"},
            ["n", "origin", "dest"],
        )
        self.assertEqual(out, "订2张到北京的票")

    def test_segment_needs_every_referenced_slot(self) -> None:
        # Space inside the bracket, as docs §2 prescribes ("把可选槽的周边措辞
        # （含空格/标点）都放进方括号内") — the segment then drops cleanly.
        tpl = "Book a seat[ on {train} in {seat_class}]."
        names = ["train", "seat_class"]
        self.assertEqual(_fill_template(tpl, {"train": "G1"}, names), "Book a seat.")
        self.assertEqual(
            _fill_template(tpl, {"train": "G1", "seat_class": "business"}, names),
            "Book a seat on G1 in business.",
        )

    def test_space_outside_the_bracket_survives_the_drop(self) -> None:
        # Authoring caveat: the surrounding space must live INSIDE the segment.
        # Left outside, dropping the segment strands it (only runs of 2+ spaces
        # are tidied), which is why the load-time validator exists.
        self.assertEqual(
            _fill_template("Book [a {seat} seat].", {}, ["seat"]), "Book ."
        )

    def test_brackets_without_declared_slot_stay_literal(self) -> None:
        self.assertEqual(
            _fill_template("Search [sic] for {q}.", {"q": "cats"}, ["q"]),
            "Search [sic] for cats.",
        )

    def test_bare_optional_slot_is_blanked_not_segment_dropped(self) -> None:
        # Documented caveat: an un-bracketed optional slot leaves a gap; the
        # double-space tidy-up is what keeps it readable.
        self.assertEqual(
            _fill_template("Go to {place} by {mode}.", {"place": "HKIA"}, ["place", "mode"]),
            "Go to HKIA by .",
        )

    def test_cross_step_var_token_survives_as_a_slot_value(self) -> None:
        # A slot value may itself be an upstream {var} token — targeted
        # replacement (not str.format) must leave it intact for runtime render().
        self.assertEqual(
            _fill_template("导航去{place}", {"place": "{poi.name}"}, ["place"]),
            "导航去{poi.name}",
        )

    def test_undeclared_braces_are_left_alone(self) -> None:
        self.assertEqual(
            _fill_slots("keep {other} fill {q}", {"q": "x", "other": "y"}, ["q"]),
            "keep {other} fill x",
        )

    def test_numeric_zero_counts_as_a_value(self) -> None:
        self.assertTrue(_has_slot_value({"n": 0}, "n"))
        self.assertFalse(_has_slot_value({"n": "  "}, "n"))
        self.assertFalse(_has_slot_value({}, "n"))
        self.assertEqual(_fill_template("买{n}张", {"n": 0}, ["n"]), "买0张")


# --------------------------------------------------------------------------- #
# FlowPlanner._fill_prompt_template (LLM slot extraction + guards)
# --------------------------------------------------------------------------- #


class FillPromptTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        # _fill_prompt_template loads the user profile (M2②) — isolate it.
        self._tmp = tempfile.mkdtemp()
        os.environ["RELAY_PROFILE_ROOT"] = self._tmp
        self.planner = FlowPlanner({"apps": []}, MagicMock(), "test-model")

    def tearDown(self) -> None:
        os.environ.pop("RELAY_PROFILE_ROOT", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    @staticmethod
    def _cap(required_mode: bool = False) -> dict:
        return {
            "prompt_template": "Navigate to {place}[ by {mode}].",
            "prompt_slots": [
                {"name": "place", "desc": "destination", "required": True},
                {"name": "mode", "desc": "travel mode", "required": required_mode},
            ],
        }

    def _fill(self, slot_reply: dict, cap: dict | None = None, produced=frozenset()):
        import json as _json

        body = f"```json\n{_json.dumps(slot_reply, ensure_ascii=False)}\n```"
        resp = MagicMock(choices=[MagicMock(message=MagicMock(content=body))])
        with patch("agents.flow.flow_planner.create_with_retry", return_value=resp):
            return self.planner._fill_prompt_template(
                cap or self._cap(), "synthesized prompt", "user request", set(produced)
            )

    def test_fills_required_slot(self) -> None:
        out = self._fill({"slots": {"place": "HKIA"}})
        self.assertEqual(out, "Navigate to HKIA.")

    def test_fills_optional_segment(self) -> None:
        out = self._fill({"slots": {"place": "HKIA", "mode": "driving"}})
        self.assertEqual(out, "Navigate to HKIA by driving.")

    def test_missing_required_slot_is_hard_failure(self) -> None:
        with self.assertRaises(PromptTemplateError) as ctx:
            self._fill({"slots": {"mode": "driving"}, "missing": ["place"]})
        self.assertIn("place", str(ctx.exception))

    def test_blank_required_slot_is_hard_failure(self) -> None:
        with self.assertRaises(PromptTemplateError):
            self._fill({"slots": {"place": "   "}})

    def test_non_object_extractor_reply_is_hard_failure(self) -> None:
        with self.assertRaises(PromptTemplateError):
            self._fill(["not", "an", "object"])

    def test_non_object_slots_field_is_hard_failure(self) -> None:
        with self.assertRaises(PromptTemplateError):
            self._fill({"slots": ["place"]})

    def test_upstream_var_token_allowed_when_produced(self) -> None:
        out = self._fill({"slots": {"place": "{poi.name}"}}, produced={"poi"})
        self.assertEqual(out, "Navigate to {poi.name}.")

    def test_unbound_var_token_is_hard_failure(self) -> None:
        # render() would silently drop it to '' at runtime — reject at plan time.
        with self.assertRaises(PromptTemplateError) as ctx:
            self._fill({"slots": {"place": "{poi.name}"}}, produced=set())
        self.assertIn("poi", str(ctx.exception))


# --------------------------------------------------------------------------- #
# card_catalog._validate_prompt_template (load-time authoring contract)
# --------------------------------------------------------------------------- #


class ValidatePromptTemplateTests(unittest.TestCase):
    @staticmethod
    def _slots(*specs) -> list[dict]:
        return [{"name": n, "required": r} for n, r in specs]

    def _errors(self, template: str, *specs) -> list[str]:
        return _validate_prompt_template("app", "cap", template, self._slots(*specs))

    def test_valid_template_has_no_errors(self) -> None:
        self.assertEqual(
            self._errors("Navigate to {place}[ by {mode}].",
                         ("place", True), ("mode", False)),
            [],
        )

    def test_template_without_optional_slots_is_valid(self) -> None:
        self.assertEqual(self._errors("导航去{place}", ("place", True)), [])

    def test_undeclared_placeholder_is_rejected(self) -> None:
        errors = self._errors("Navigate to {palce}.", ("place", True))
        self.assertTrue(any("undeclared slot {palce}" in e for e in errors))

    def test_dead_slot_is_rejected(self) -> None:
        errors = self._errors("Navigate to {place}.", ("place", True), ("mode", False))
        self.assertTrue(any("'mode' declared but never used" in e for e in errors))

    def test_required_slot_only_inside_segment_is_rejected(self) -> None:
        errors = self._errors("Navigate[ to {place}].", ("place", True))
        self.assertTrue(any("would be droppable" in e for e in errors))

    def test_slot_used_both_inside_and_outside_a_segment(self) -> None:
        # `outside_segment` is derived by POSITION, so an occurrence in each
        # place puts the slot in both sets: a required slot's outside
        # occurrence keeps it undroppable, and an optional slot's outside
        # occurrence still trips the wrapping check.
        required = self._errors("Go {place}[ near {place}].", ("place", True))
        self.assertEqual(required, [])
        optional = self._errors("Go {place} {mode}[ by {mode}].",
                                ("place", True), ("mode", False))
        self.assertTrue(any("must be wrapped in a '[...]' segment" in e for e in optional))

    def test_optional_slot_outside_a_segment_is_rejected(self) -> None:
        errors = self._errors("Navigate to {place} by {mode}.",
                              ("place", True), ("mode", False))
        self.assertTrue(any("must be wrapped in a '[...]' segment" in e for e in errors))

    def test_unbalanced_brackets_are_rejected(self) -> None:
        errors = self._errors("Navigate to {place}[ by {mode}.",
                              ("place", True), ("mode", False))
        self.assertEqual(len(errors), 1)
        self.assertIn("unbalanced or nested", errors[0])

    def test_nested_brackets_are_rejected(self) -> None:
        errors = self._errors("Go to {place}[ by {mode}[ fast]].",
                              ("place", True), ("mode", False))
        self.assertEqual(len(errors), 1)
        self.assertIn("unbalanced or nested", errors[0])

    def test_error_message_names_the_capability(self) -> None:
        errors = _validate_prompt_template(
            "com.example.app", "live_navigation", "Go to {palce}.",
            [{"name": "place", "required": True}],
        )
        self.assertTrue(all(e.startswith("com.example.app/live_navigation:") for e in errors))


if __name__ == "__main__":
    unittest.main()
