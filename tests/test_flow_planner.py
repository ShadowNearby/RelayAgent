"""Unit tests for FlowPlanner validation, repair, and fallback legs
(MobileWorld + general)."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from agents.flow.flow_planner import (
    GENERAL_STEP_TYPE,
    MW_STEP_TYPE,
    FlowPlanner,
    PlanValidationError,
    _general_whole_request_plan,
    _mw_whole_request_plan,
    _to_general_leg,
    _to_mw_leg,
)


class _ProfileIsolatedTestCase(unittest.TestCase):
    """Base for tests that reach planner.plan() / _fill_prompt_template —
    both load the user profile, so the store is pointed at a throwaway dir
    (never the developer's real ~/.relayagent/profile.yaml)."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        os.environ["RELAY_PROFILE_ROOT"] = self._tmp

    def tearDown(self) -> None:
        os.environ.pop("RELAY_PROFILE_ROOT", None)
        shutil.rmtree(self._tmp, ignore_errors=True)


def _minimal_catalog() -> dict:
    return {
        "apps": [
            {
                "app_id": "com.example.app",
                "capabilities": [
                    {"id": "foundation_llm", "handoff_to_user_required": False},
                    {"id": "handoff_cap", "handoff_to_user_required": True},
                    {"id": "no_reply_cap", "x_skip_wait_for_reply": True},
                ],
            }
        ]
    }


def _unrepairable_plan() -> dict:
    """A plan that never validates: a handoff capability that is not the final
    step and is not followed by an ask_user, plus a downstream unbound var."""
    return {
        "steps": [
            {"id": "s1", "app": "com.example.app", "capability": "handoff_cap",
             "prompt": "do thing"},
            {"id": "s2", "app": "com.example.app", "capability": "foundation_llm",
             "prompt": "follow up {choice}"},
        ]
    }


def _llm_response(plan: dict) -> MagicMock:
    body = f"```json\n{json.dumps(plan, ensure_ascii=False)}\n```"
    return MagicMock(choices=[MagicMock(message=MagicMock(content=body))])


class FlowPlannerValidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = FlowPlanner(_minimal_catalog(), MagicMock(), "test-model")

    def test_rejects_non_string_select_from(self) -> None:
        plan = {
            "steps": [
                {
                    "id": "ask",
                    "type": "ask_user",
                    "bind": "choice",
                    "select_from": ["items"],
                }
            ]
        }
        errors = self.planner._validate(plan)
        self.assertTrue(any("select_from must be a single var name string" in e for e in errors))

    def test_accepts_braced_select_from_when_bound(self) -> None:
        plan = {
            "steps": [
                {
                    "id": "search",
                    "app": "com.example.app",
                    "capability": "foundation_llm",
                    "prompt": "find places",
                    "bind": "items",
                },
                {
                    "id": "ask",
                    "type": "ask_user",
                    "bind": "choice",
                    "select_from": "{items}",
                    "prompt_header": "pick one",
                },
            ]
        }
        self.assertEqual(self.planner._validate(plan), [])

    def test_mw_leg_valid_without_app_or_capability(self) -> None:
        plan = {
            "steps": [
                {
                    "id": "mw",
                    "type": MW_STEP_TYPE,
                    "prompt": "open settings and enable wifi",
                    "x_fallback_reason": "coverage gap",
                }
            ]
        }
        self.assertEqual(self.planner._validate(plan), [])

    def test_mw_leg_rejects_unbound_var_reference(self) -> None:
        plan = {
            "steps": [
                {
                    "id": "mw",
                    "type": MW_STEP_TYPE,
                    "prompt": "navigate to {place}",
                }
            ]
        }
        errors = self.planner._validate(plan)
        self.assertTrue(any("references {place}" in e for e in errors))

    def test_missing_step_id_is_validation_error(self) -> None:
        # The runner hard-reads step['id'] (logging, leg dir names): a plan
        # with a missing id must fail validation, not KeyError at execution.
        plan = {
            "steps": [
                {"app": "com.example.app", "capability": "foundation_llm",
                 "prompt": "do thing"},
                {"type": "ask_user", "bind": "x", "prompt_header": "?"},
            ]
        }
        errors = self.planner._validate(plan)
        self.assertTrue(any("step #0: missing `id`" in e for e in errors))
        self.assertTrue(any("step #1: missing `id`" in e for e in errors))

    def test_extract_requires_nonempty_prompt(self) -> None:
        plan = {
            "steps": [
                {"id": "s1", "app": "com.example.app",
                 "capability": "foundation_llm", "prompt": "p",
                 "extract": {"bind_to_array_key": "items"}, "bind": "x"},
                {"id": "mw", "type": MW_STEP_TYPE, "prompt": "p",
                 "extract": "parse it", "bind": "y"},
            ]
        }
        errors = self.planner._validate(plan)
        self.assertTrue(any("s1" in e and "`extract`" in e for e in errors))
        self.assertTrue(any("mw" in e and "`extract`" in e for e in errors))

    def test_drop_unused_no_reply_binds_tolerates_non_string_bind(self) -> None:
        # A list bind on a no-reply-capability step must be left for _validate
        # to flag (repairable), not TypeError inside the drop helper.
        plan = {
            "steps": [
                {"id": "s1", "app": "com.example.app", "capability": "no_reply_cap",
                 "prompt": "go", "bind": ["a", "b"]},
                {"id": "s2", "app": "com.example.app",
                 "capability": "foundation_llm", "prompt": "next"},
            ]
        }
        self.planner._drop_unused_no_reply_binds(plan)  # must not raise
        errors = self.planner._validate(plan)
        self.assertTrue(
            any("bind must be a single var name string" in e for e in errors)
        )


class FlowPlannerMwFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = FlowPlanner(
            _minimal_catalog(), MagicMock(), "test-model", mw_fallback=True
        )

    def test_to_mw_leg_converts_in_place(self) -> None:
        step = {
            "id": "gap",
            "app": "com.example.app",
            "capability": "missing_cap",
            "prompt": "do something",
            "x_coverage_gap": "no app",
            "x_route_key": "abc",
        }
        _to_mw_leg(step, "no runnable app")
        self.assertEqual(step["type"], MW_STEP_TYPE)
        self.assertNotIn("capability", step)
        self.assertNotIn("x_coverage_gap", step)
        self.assertNotIn("x_route_key", step)
        self.assertEqual(step["x_fallback_reason"], "no runnable app")
        self.assertEqual(step["app"], "com.example.app")

    def test_mw_whole_request_plan_shape(self) -> None:
        plan = _mw_whole_request_plan("book a table", "no catalog coverage")
        self.assertEqual(len(plan["steps"]), 1)
        self.assertEqual(plan["steps"][0]["type"], MW_STEP_TYPE)
        self.assertEqual(plan["steps"][0]["prompt"], "book a table")

    def test_apply_fallback_to_gaps(self) -> None:
        plan = {
            "steps": [
                {
                    "id": "gap",
                    "prompt": "find foo",
                    "x_coverage_gap": "no app for cap",
                }
            ]
        }
        out = self.planner._apply_fallback_to_gaps(plan, "find foo", "reason")
        self.assertEqual(out["steps"][0]["type"], MW_STEP_TYPE)
        self.assertEqual(self.planner._validate(out), [])


class FlowPlannerGeneralFallbackTests(unittest.TestCase):
    """MW off + general on: uncovered legs fall to the manifest-free general
    agent instead of MobileWorld; both off restores the unsatisfiable path."""

    def _planner(self, *, mw: bool, general: bool) -> FlowPlanner:
        return FlowPlanner(
            _minimal_catalog(), MagicMock(), "test-model",
            mw_fallback=mw, general_fallback=general,
        )

    def test_to_general_leg_converts_in_place(self) -> None:
        step = {
            "id": "gap",
            "app": "com.example.app",
            "capability": "missing_cap",
            "prompt": "do something",
            "x_coverage_gap": "no app",
            "x_route_key": "abc",
        }
        _to_general_leg(step, "no runnable app")
        self.assertEqual(step["type"], GENERAL_STEP_TYPE)
        self.assertNotIn("capability", step)
        self.assertNotIn("x_coverage_gap", step)
        self.assertNotIn("x_route_key", step)
        self.assertEqual(step["x_fallback_reason"], "no runnable app")
        self.assertEqual(step["app"], "com.example.app")  # kept as launch hint

    def test_general_whole_request_plan_shape(self) -> None:
        plan = _general_whole_request_plan("book a table", "no coverage")
        self.assertEqual(len(plan["steps"]), 1)
        self.assertEqual(plan["steps"][0]["type"], GENERAL_STEP_TYPE)
        self.assertEqual(plan["steps"][0]["prompt"], "book a table")

    def test_general_leg_valid_without_app_or_capability(self) -> None:
        planner = self._planner(mw=False, general=True)
        plan = {
            "steps": [
                {
                    "id": "g",
                    "type": GENERAL_STEP_TYPE,
                    "prompt": "open settings and enable wifi",
                    "x_fallback_reason": "coverage gap",
                }
            ]
        }
        self.assertEqual(planner._validate(plan), [])

    def test_gaps_fall_to_general_when_mw_off(self) -> None:
        planner = self._planner(mw=False, general=True)
        plan = {
            "steps": [
                {"id": "gap", "prompt": "find foo", "x_coverage_gap": "no app"}
            ]
        }
        out = planner._apply_fallback_to_gaps(plan, "find foo", "reason")
        self.assertEqual(out["steps"][0]["type"], GENERAL_STEP_TYPE)
        self.assertEqual(planner._validate(out), [])

    def test_mw_takes_priority_over_general(self) -> None:
        planner = self._planner(mw=True, general=True)
        plan = {
            "steps": [
                {"id": "gap", "prompt": "find foo", "x_coverage_gap": "no app"}
            ]
        }
        out = planner._apply_fallback_to_gaps(plan, "find foo", "reason")
        self.assertEqual(out["steps"][0]["type"], MW_STEP_TYPE)

    def test_whole_request_fallback_priority_and_off(self) -> None:
        general = self._planner(mw=False, general=True)._whole_request_fallback(
            "req", "reason", "planner"
        )
        self.assertEqual(general["steps"][0]["type"], GENERAL_STEP_TYPE)
        mw = self._planner(mw=True, general=True)._whole_request_fallback(
            "req", "reason", "planner"
        )
        self.assertEqual(mw["steps"][0]["type"], MW_STEP_TYPE)
        neither = self._planner(mw=False, general=False)._whole_request_fallback(
            "req", "reason", "planner"
        )
        self.assertIsNone(neither)


class FlowPlannerFoundationFallbackTests(unittest.TestCase):
    """A device action a chat assistant can't do must not be forced into
    foundation_llm: stage-3 raises FoundationNotApplicable, which the planner
    turns into a coverage gap -> MobileWorld leg."""

    def setUp(self) -> None:
        self.planner = FlowPlanner(
            _minimal_catalog(), MagicMock(), "test-model", mw_fallback=True
        )

    def test_route_step_marks_foundation_not_applicable_as_gap(self) -> None:
        from agents.routing.capability_matrix_router import FoundationNotApplicable

        step = {
            "id": "rename",
            "prompt": "rename bid_ files in Download by creation date",
        }
        errors: list[str] = []
        gaps: list[str] = []
        with patch(
            "agents.flow.flow_planner.route_app_capability",
            side_effect=FoundationNotApplicable("file rename is a device action"),
        ):
            self.planner._route_one_step(step, 0, "rename files", set(), errors, gaps)
        self.assertIn("x_coverage_gap", step)
        self.assertTrue(gaps)
        # the gap then resolves to a MobileWorld leg
        out = self.planner._apply_fallback_to_gaps({"steps": [step]}, "rename files", "reason")
        self.assertEqual(out["steps"][0]["type"], MW_STEP_TYPE)
        self.assertEqual(self.planner._validate(out), [])


class FlowPlannerRepairTests(_ProfileIsolatedTestCase):
    def test_plan_repairs_validation_error(self) -> None:
        bad = {
            "steps": [
                {
                    "id": "s1",
                    "app": "com.example.app",
                    "capability": "handoff_cap",
                    "prompt": "do thing",
                },
                {
                    "id": "s2",
                    "app": "com.example.app",
                    "capability": "foundation_llm",
                    "prompt": "follow up {choice}",
                },
            ]
        }
        good = {
            "steps": [
                {
                    "id": "s1",
                    "app": "com.example.app",
                    "capability": "handoff_cap",
                    "prompt": "do thing",
                },
                {
                    "id": "ask",
                    "type": "ask_user",
                    "bind": "choice",
                    "prompt_header": "pick",
                },
                {
                    "id": "s2",
                    "app": "com.example.app",
                    "capability": "foundation_llm",
                    "prompt": "follow up {choice}",
                },
            ]
        }
        responses = [_llm_response(bad), _llm_response(good)]
        with patch("agents.flow.flow_planner.create_with_retry", side_effect=responses):
            planner = FlowPlanner(_minimal_catalog(), MagicMock(), "test-model")
            result = planner.plan("test request")
        self.assertEqual(planner._validate(result), [])
        self.assertEqual(len(result["steps"]), 3)

    def test_unrepairable_plan_with_mw_fallback_becomes_whole_request_mw_leg(self) -> None:
        # A plan that stays invalid through every repair round (here: a handoff
        # capability that is NOT the final step and is never followed by an
        # ask_user, plus a downstream unbound var) must NOT raise
        # PlanValidationError when MW fallback is on — it falls back to a
        # whole-request MobileWorld leg instead of giving up.
        bad = _unrepairable_plan()
        # planner.plan: 1 synth + _REPAIR_ROUNDS repairs, all returning `bad`.
        responses = [_llm_response(bad)] * 8
        with patch("agents.flow.flow_planner.create_with_retry", side_effect=responses):
            planner = FlowPlanner(
                _minimal_catalog(), MagicMock(), "test-model", mw_fallback=True
            )
            result = planner.plan("do an unrepairable thing")
        self.assertEqual(result["steps"][0]["type"], MW_STEP_TYPE)
        self.assertEqual(result["steps"][0]["prompt"], "do an unrepairable thing")

    def test_unrepairable_plan_with_mw_off_falls_to_general_leg(self) -> None:
        # MW off but general fallback on (the on-device configuration): an
        # unrepairable plan becomes a whole-request GENERAL leg, not a raise.
        bad = _unrepairable_plan()
        responses = [_llm_response(bad)] * 8
        with patch("agents.flow.flow_planner.create_with_retry", side_effect=responses):
            planner = FlowPlanner(
                _minimal_catalog(), MagicMock(), "test-model",
                mw_fallback=False, general_fallback=True,
            )
            result = planner.plan("do an unrepairable thing")
        self.assertEqual(result["steps"][0]["type"], GENERAL_STEP_TYPE)
        self.assertEqual(result["steps"][0]["prompt"], "do an unrepairable thing")

    def test_unrepairable_plan_without_any_fallback_still_raises(self) -> None:
        bad = _unrepairable_plan()
        responses = [_llm_response(bad)] * 8
        with patch("agents.flow.flow_planner.create_with_retry", side_effect=responses):
            planner = FlowPlanner(
                _minimal_catalog(), MagicMock(), "test-model",
                mw_fallback=False, general_fallback=False,
            )
            with self.assertRaises(PlanValidationError):
                planner.plan("do an unrepairable thing")

    def test_unsatisfiable_with_mw_fallback_becomes_whole_request_mw_leg(self) -> None:
        payload = {"unsatisfiable": True, "reason": "needs camera"}
        with patch(
            "agents.flow.flow_planner.create_with_retry",
            return_value=_llm_response(payload),
        ):
            planner = FlowPlanner(
                _minimal_catalog(), MagicMock(), "test-model", mw_fallback=True
            )
            result = planner.plan("take a photo")
        self.assertEqual(result["steps"][0]["type"], MW_STEP_TYPE)
        self.assertEqual(result["steps"][0]["prompt"], "take a photo")


class PlannerJsonParseFailureTests(_ProfileIsolatedTestCase):
    """A planner/repair reply that is not parseable JSON (max_tokens truncation,
    prose, a second fence) must feed the repair loop — never escape plan() as a
    bare JSONDecodeError (nl_flow.plan_request only catches PlanValidationError,
    so it would crash the CLI / Android entry)."""

    @staticmethod
    def _raw(body: str) -> MagicMock:
        return MagicMock(choices=[MagicMock(message=MagicMock(content=body))])

    def _good_plan(self) -> dict:
        return {
            "steps": [
                {"id": "s1", "app": "com.example.app",
                 "capability": "foundation_llm", "prompt": "do thing"}
            ]
        }

    def test_truncated_synthesis_is_repaired(self) -> None:
        truncated = self._raw('```json\n{"steps": [{"id": "s1", "prom')
        with patch(
            "agents.flow.flow_planner.create_with_retry",
            side_effect=[truncated, _llm_response(self._good_plan())],
        ):
            planner = FlowPlanner(_minimal_catalog(), MagicMock(), "test-model")
            result = planner.plan("test request")
        self.assertEqual(planner._validate(result), [])
        self.assertEqual(result["steps"][0]["id"], "s1")

    def test_unparseable_throughout_raises_plan_validation_error(self) -> None:
        junk = self._raw("抱歉，我无法给出计划。")
        with patch("agents.flow.flow_planner.create_with_retry", return_value=junk):
            planner = FlowPlanner(
                _minimal_catalog(), MagicMock(), "test-model",
                mw_fallback=False, general_fallback=False,
            )
            with self.assertRaises(PlanValidationError):
                planner.plan("test request")

    def test_unparseable_throughout_falls_back_when_enabled(self) -> None:
        junk = self._raw("抱歉，我无法给出计划。")
        with patch("agents.flow.flow_planner.create_with_retry", return_value=junk):
            planner = FlowPlanner(
                _minimal_catalog(), MagicMock(), "test-model", mw_fallback=True
            )
            result = planner.plan("do the thing")
        self.assertEqual(result["steps"][0]["type"], MW_STEP_TYPE)
        self.assertEqual(result["steps"][0]["prompt"], "do the thing")

    def test_unparseable_repair_reply_keeps_previous_plan(self) -> None:
        # Synthesis is valid JSON but invalid; the first repair reply is junk
        # (that round is burned), the second repairs it properly.
        bad = _unrepairable_plan()
        good = {
            "steps": [
                {"id": "s1", "app": "com.example.app",
                 "capability": "foundation_llm", "prompt": "do thing"}
            ]
        }
        with patch(
            "agents.flow.flow_planner.create_with_retry",
            side_effect=[
                _llm_response(bad),
                self._raw("oops, not json"),
                _llm_response(good),
            ],
        ):
            planner = FlowPlanner(_minimal_catalog(), MagicMock(), "test-model")
            result = planner.plan("test request")
        self.assertEqual(planner._validate(result), [])
        self.assertEqual(len(result["steps"]), 1)


class CoverageGapMarkerTests(unittest.TestCase):
    """`x_coverage_gap` must reflect the FINAL round's gaps only: a step that
    routes successfully this round must not keep a stale marker echoed back by
    a repair round, or _apply_fallback_to_gaps would convert a healthy step
    (throwing away its routed capability and route key)."""

    def setUp(self) -> None:
        self.planner = FlowPlanner(
            _minimal_catalog(), MagicMock(), "test-model", mw_fallback=True
        )

    def test_successful_route_clears_stale_gap_marker(self) -> None:
        step = {
            "id": "s1",
            "prompt": "find a bookstore",
            # echoed back by a repair round after the previous round's gap
            "x_coverage_gap": "no app for cap (stale)",
        }
        errors: list[str] = []
        gaps: list[str] = []
        with patch(
            "agents.flow.flow_planner.route_app_capability",
            return_value={"app_id": "com.example.app",
                          "capability_id": "foundation_llm", "reason": "ok"},
        ):
            self.planner._route_one_step(step, 0, "find a bookstore", set(), errors, gaps)
        self.assertNotIn("x_coverage_gap", step)
        self.assertEqual(gaps, [])
        self.assertEqual(step["capability"], "foundation_llm")

    def test_repaired_step_is_not_converted_with_a_still_gapped_sibling(self) -> None:
        from agents.routing.capability_matrix_router import NoRunnableAppForCapability

        repaired = {"id": "was_gap", "prompt": "now routable",
                    "x_coverage_gap": "stale marker from round 1"}
        still_gap = {"id": "gap", "prompt": "uncoverable"}
        errors: list[str] = []
        gaps: list[str] = []
        with patch(
            "agents.flow.flow_planner.route_app_capability",
            side_effect=[
                {"app_id": "com.example.app", "capability_id": "foundation_llm",
                 "reason": "ok"},
                NoRunnableAppForCapability("no app authorized"),
            ],
        ):
            self.planner._route_one_step(repaired, 0, "req", set(), errors, gaps)
            self.planner._route_one_step(still_gap, 1, "req", set(), errors, gaps)
        plan = {"steps": [repaired, still_gap]}
        out = self.planner._apply_fallback_to_gaps(plan, "req", "reason")
        # only the genuinely uncovered step became a fallback leg
        self.assertNotIn("type", out["steps"][0])
        self.assertEqual(out["steps"][0]["capability"], "foundation_llm")
        self.assertEqual(out["steps"][1]["type"], MW_STEP_TYPE)


class PlanValidationErrorTests(unittest.TestCase):
    def test_coverage_gaps_field(self) -> None:
        err = PlanValidationError(
            "req",
            {},
            ["routing failed"],
            coverage_gaps=["step gap: no app"],
        )
        self.assertEqual(err.coverage_gaps, ["step gap: no app"])


if __name__ == "__main__":
    unittest.main()
