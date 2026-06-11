"""Unit tests for FlowPlanner validation, repair, and MobileWorld fallback."""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from agents.flow_planner import (
    MW_STEP_TYPE,
    FlowPlanner,
    PlanValidationError,
    _mw_whole_request_plan,
    _to_mw_leg,
)


def _minimal_catalog() -> dict:
    return {
        "apps": [
            {
                "app_id": "com.example.app",
                "capabilities": [
                    {"id": "foundation_llm", "handoff_to_user_required": False},
                    {"id": "handoff_cap", "handoff_to_user_required": True},
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

    def test_apply_mw_fallback_to_gaps(self) -> None:
        plan = {
            "steps": [
                {
                    "id": "gap",
                    "prompt": "find foo",
                    "x_coverage_gap": "no app for cap",
                }
            ]
        }
        out = self.planner._apply_mw_fallback_to_gaps(plan, "find foo", "reason")
        self.assertEqual(out["steps"][0]["type"], MW_STEP_TYPE)
        self.assertEqual(self.planner._validate(out), [])


class FlowPlannerFoundationFallbackTests(unittest.TestCase):
    """A device action a chat assistant can't do must not be forced into
    foundation_llm: stage-3 raises FoundationNotApplicable, which the planner
    turns into a coverage gap -> MobileWorld leg."""

    def setUp(self) -> None:
        self.planner = FlowPlanner(
            _minimal_catalog(), MagicMock(), "test-model", mw_fallback=True
        )

    def test_route_step_marks_foundation_not_applicable_as_gap(self) -> None:
        from agents.capability_matrix_router import FoundationNotApplicable

        step = {
            "id": "rename",
            "prompt": "rename bid_ files in Download by creation date",
        }
        errors: list[str] = []
        gaps: list[str] = []
        with patch(
            "agents.flow_planner.route_app_capability",
            side_effect=FoundationNotApplicable("file rename is a device action"),
        ):
            self.planner._route_one_step(step, 0, "rename files", set(), errors, gaps)
        self.assertIn("x_coverage_gap", step)
        self.assertTrue(gaps)
        # the gap then resolves to a MobileWorld leg
        out = self.planner._apply_mw_fallback_to_gaps({"steps": [step]}, "rename files", "reason")
        self.assertEqual(out["steps"][0]["type"], MW_STEP_TYPE)
        self.assertEqual(self.planner._validate(out), [])


class FlowPlannerRepairTests(unittest.TestCase):
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
        with patch("agents.flow_planner.create_with_retry", side_effect=responses):
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
        with patch("agents.flow_planner.create_with_retry", side_effect=responses):
            planner = FlowPlanner(
                _minimal_catalog(), MagicMock(), "test-model", mw_fallback=True
            )
            result = planner.plan("do an unrepairable thing")
        self.assertEqual(result["steps"][0]["type"], MW_STEP_TYPE)
        self.assertEqual(result["steps"][0]["prompt"], "do an unrepairable thing")

    def test_unrepairable_plan_without_mw_fallback_still_raises(self) -> None:
        bad = _unrepairable_plan()
        responses = [_llm_response(bad)] * 8
        with patch("agents.flow_planner.create_with_retry", side_effect=responses):
            planner = FlowPlanner(
                _minimal_catalog(), MagicMock(), "test-model", mw_fallback=False
            )
            with self.assertRaises(PlanValidationError):
                planner.plan("do an unrepairable thing")

    def test_unsatisfiable_with_mw_fallback_becomes_whole_request_mw_leg(self) -> None:
        payload = {"unsatisfiable": True, "reason": "needs camera"}
        with patch(
            "agents.flow_planner.create_with_retry",
            return_value=_llm_response(payload),
        ):
            planner = FlowPlanner(
                _minimal_catalog(), MagicMock(), "test-model", mw_fallback=True
            )
            result = planner.plan("take a photo")
        self.assertEqual(result["steps"][0]["type"], MW_STEP_TYPE)
        self.assertEqual(result["steps"][0]["prompt"], "take a photo")


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
