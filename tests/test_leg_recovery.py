"""Unit tests for the runtime failure-recovery ladder (roadmap P1).

Fault-injects scripted `LegResult`s in place of real device legs, pinning:
the failure taxonomy (R0), tier order retry → reroute → MW (R1–R3), the
budget guardrails, the handoff-capability retry-only red line, and the
RELAY_RECOVERY=0 fail-fast compatibility path.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

from agents.flow.flow_runner import FlowRunner, LegResult
from agents.flow.leg_judge import LegVerdict, _parse_verdict
from agents.flow.leg_recovery import (
    APP_FAIL,
    ENV_FAIL,
    ROUTE_FAIL,
    RecoveryController,
    classify_leg_failure,
)

# Placeholder leg dir for results that don't need a real one. A real throwaway
# dir (not Path(".")): the ladder writes recovery.json next to the first
# attempt's trajectory, and a cwd placeholder would litter the repo root.
_D = Path(tempfile.mkdtemp(prefix="leg_recovery_test_"))


def _ok(reply: str = "done") -> LegResult:
    return LegResult(rc=0, reply=reply, summary={"last_action_type": "answer"},
                     needs_reply=True, leg_dir=_D, prompt="p")


def _judged_fail(kind: str = "app_error") -> LegResult:
    return LegResult(rc=0, reply="wrong stuff", summary={"token_usage": {"total_tokens": 100}},
                     needs_reply=True, leg_dir=_D, prompt="p",
                     verdict=LegVerdict("failure", "did not deliver", kind))


def _hard_fail() -> LegResult:
    return LegResult(rc=0, reply="", summary={"last_action_type": "finished"},
                     needs_reply=True, hard_error="no reply captured", leg_dir=_D,
                     prompt="p")


def _env_fail() -> LegResult:
    return LegResult(rc=1, reply="", summary={}, needs_reply=True, leg_dir=_D, prompt="p")


class ClassifyTests(unittest.TestCase):
    def test_env_fail_when_subprocess_died_without_summary(self) -> None:
        f = classify_leg_failure(1, {}, "", True, None, None)
        self.assertEqual(f.kind, ENV_FAIL)
        self.assertTrue(f.fatal)

    def test_missing_needed_reply_is_fatal_app_fail(self) -> None:
        f = classify_leg_failure(0, {"a": 1}, "", True, "no reply captured", None)
        self.assertEqual(f.kind, APP_FAIL)
        self.assertTrue(f.fatal)

    def test_judge_wrong_feature_is_route_fail_nonfatal(self) -> None:
        v = LegVerdict("failure", "answered something else", "wrong_feature")
        f = classify_leg_failure(0, {"a": 1}, "text", True, None, v)
        self.assertEqual(f.kind, ROUTE_FAIL)
        self.assertFalse(f.fatal)

    def test_judge_app_error_is_app_fail(self) -> None:
        v = LegVerdict("failure", "risk-control wall", "app_error")
        f = classify_leg_failure(0, {"a": 1}, "text", True, None, v)
        self.assertEqual(f.kind, APP_FAIL)

    def test_success_and_loading_are_not_failures(self) -> None:
        self.assertIsNone(classify_leg_failure(0, {"a": 1}, "text", True, None,
                                               LegVerdict("success", "ok")))
        self.assertIsNone(classify_leg_failure(0, {"a": 1}, "text", True, None,
                                               LegVerdict("loading", "still going")))
        # rc != 0 but reply captured + summary present: accepted (today's rule)
        self.assertIsNone(classify_leg_failure(1, {"a": 1}, "text", True, None, None))


class JudgeFailureKindParseTests(unittest.TestCase):
    def test_parses_failure_kind(self) -> None:
        raw = '```json\n{"status": "failure", "reason": "r", "failure_kind": "wrong_feature"}\n```'
        self.assertEqual(_parse_verdict(raw), ("failure", "r", "wrong_feature"))

    def test_drops_kind_on_success(self) -> None:
        raw = '{"status": "success", "reason": "r", "failure_kind": "app_error"}'
        self.assertEqual(_parse_verdict(raw), ("success", "r", ""))

    def test_drops_invalid_kind(self) -> None:
        raw = '{"status": "failure", "reason": "r", "failure_kind": "meltdown"}'
        self.assertEqual(_parse_verdict(raw), ("failure", "r", ""))

    def test_salvages_kind_from_truncated_output(self) -> None:
        raw = '```json\n{"status": "failure", "failure_kind": "app_error", "reason": "the app sho'
        self.assertEqual(_parse_verdict(raw), ("failure", "the app sho", "app_error"))


class BudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["RELAY_RECOVERY"] = "1"

    def tearDown(self) -> None:
        for k in ("RELAY_RECOVERY", "RELAY_RECOVERY_MAX_LEGS",
                  "RELAY_RECOVERY_TOKEN_BUDGET"):
            os.environ.pop(k, None)

    def test_leg_and_token_budgets(self) -> None:
        os.environ["RELAY_RECOVERY_MAX_LEGS"] = "2"
        os.environ["RELAY_RECOVERY_TOKEN_BUDGET"] = "150"
        rec = RecoveryController(MagicMock(), "qwen")
        self.assertTrue(rec.can_spend_leg())
        rec.spend_leg({"token_usage": {"total_tokens": 100}})
        self.assertTrue(rec.can_spend_leg())
        rec.spend_leg({"token_usage": {"total_tokens": 100}})  # 200 > 150
        self.assertFalse(rec.can_spend_leg())
        self.assertEqual(rec.tokens_used, 200)


def _runner(tmp: str) -> FlowRunner:
    runner = FlowRunner.__new__(FlowRunner)
    runner.bb = {}
    runner.env = {"LLM_MODEL": "qwen"}
    runner._llm = MagicMock(calls=[])
    runner.flow_traj_root = Path(tmp)
    runner._step_idx = 0
    runner._step_outcomes = []
    runner._recovery = RecoveryController(runner._llm, "qwen")
    # Hermetic: don't touch the real manifest catalog in unit tests.
    runner._recovery.handoff_required = MagicMock(return_value=False)
    runner._recovery.has_prompt_template = MagicMock(return_value=False)
    return runner


_STEP = {"id": "s1", "app": "com.a", "capability": "cap_a",
         "prompt": "do it", "bind": "out"}


class RecoveryLadderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        os.environ["RELAY_RECOVERY"] = "1"

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        for k in ("RELAY_RECOVERY", "RELAY_RECOVERY_MAX_LEGS",
                  "RELAY_RECOVERY_MAX_RETRIES"):
            os.environ.pop(k, None)

    def test_retry_recovers_hard_failure(self) -> None:
        runner = _runner(self._tmp)
        first = _hard_fail()
        first.leg_dir = Path(self._tmp) / "01_s1"
        first.leg_dir.mkdir()
        runner._execute_app_leg = MagicMock(side_effect=[first, _ok("second try")])
        runner._run_app_step(dict(_STEP))
        self.assertEqual(runner.bb["out"], "second try")
        self.assertEqual(runner._step_outcomes[-1]["status"], "recovered")
        self.assertEqual(runner._step_outcomes[-1]["recovered_via"], "retry")
        attempts = json.loads((first.leg_dir / "recovery.json").read_text())
        self.assertEqual(attempts[0]["tier"], "retry")
        self.assertEqual(attempts[0]["outcome"], "ok")
        # R4: every attempt carries its token cost (0 here — _ok has no usage)
        self.assertEqual(attempts[0]["tokens"], 0)

    def test_env_fail_never_recovers(self) -> None:
        runner = _runner(self._tmp)
        runner._execute_app_leg = MagicMock(side_effect=[_env_fail()])
        with self.assertRaises(RuntimeError):
            runner._run_app_step(dict(_STEP))
        runner._execute_app_leg.assert_called_once()

    def test_route_fail_rewords_then_reroutes(self) -> None:
        runner = _runner(self._tmp)
        rec = runner._recovery
        rec.reword = MagicMock(return_value="clearer prompt")
        rec.reroute = MagicMock(return_value={"app_id": "com.b", "capability_id": "cap_b"})
        # first fails, retry (reworded) fails again, then reroute succeeds
        runner._execute_app_leg = MagicMock(side_effect=[
            _judged_fail("wrong_feature"), _judged_fail("wrong_feature"), _ok("via B")])
        runner._run_app_step(dict(_STEP))
        self.assertEqual(runner.bb["out"], "via B")
        # reword got the failed prompt; retry used the override
        rec.reword.assert_called_once()
        _, retry_kwargs = runner._execute_app_leg.call_args_list[1]
        self.assertEqual(retry_kwargs.get("prompt_override"), "clearer prompt")
        # reroute saw the original pair excluded
        exclude = rec.reroute.call_args[0][1]
        self.assertIn(("com.a", "cap_a"), exclude)
        # the rerouted attempt ran against the new pair
        args, _ = runner._execute_app_leg.call_args_list[2]
        self.assertEqual(args[0]["app"], "com.b")
        self.assertEqual(args[0]["capability"], "cap_b")

    def test_handoff_capability_is_retry_only(self) -> None:
        runner = _runner(self._tmp)
        rec = runner._recovery
        rec.handoff_required = MagicMock(return_value=True)
        rec.reroute = MagicMock()
        runner._execute_app_leg = MagicMock(
            side_effect=[_judged_fail("app_error"), _judged_fail("app_error")])
        with mock.patch("agents.flow.flow_runner.mw_fallback_enabled", return_value=True):
            runner._run_mobileworld_step = MagicMock()
            runner._run_app_step(dict(_STEP))
        rec.reroute.assert_not_called()
        runner._run_mobileworld_step.assert_not_called()
        # judge-only failure at exhaustion: best attempt is committed, flow continues
        self.assertEqual(runner.bb["out"], "wrong stuff")
        self.assertEqual(runner._step_outcomes[-1]["status"], "judged_failed")

    def test_mw_tier_after_retry_and_reroute_fail(self) -> None:
        runner = _runner(self._tmp)
        rec = runner._recovery
        rec.reroute = MagicMock(return_value=None)  # no alternative route
        runner._execute_app_leg = MagicMock(side_effect=[_hard_fail(), _hard_fail()])
        runner._run_mobileworld_step = MagicMock()  # binds on its own
        with mock.patch("agents.flow.flow_runner.mw_fallback_enabled", return_value=True):
            runner._run_app_step(dict(_STEP))
        runner._run_mobileworld_step.assert_called_once()
        mw_step = runner._run_mobileworld_step.call_args[0][0]
        self.assertEqual(mw_step.get("type"), "mobileworld")
        self.assertEqual(runner._step_outcomes[-1]["recovered_via"], "mw_fallback")

    def test_general_tier_when_mw_unavailable(self) -> None:
        """MW off (on-device / no mw extra) + general on: the last tier is the
        manifest-free general agent instead of MobileWorld."""
        runner = _runner(self._tmp)
        rec = runner._recovery
        rec.reroute = MagicMock(return_value=None)
        runner._execute_app_leg = MagicMock(side_effect=[_hard_fail(), _hard_fail()])
        runner._run_mobileworld_step = MagicMock()
        runner._run_general_step = MagicMock()  # binds on its own
        with mock.patch("agents.flow.flow_runner.mw_fallback_enabled", return_value=False), \
             mock.patch("agents.flow.flow_runner.general_fallback_enabled", return_value=True):
            runner._run_app_step(dict(_STEP))
        runner._run_mobileworld_step.assert_not_called()
        runner._run_general_step.assert_called_once()
        g_step = runner._run_general_step.call_args[0][0]
        self.assertEqual(g_step.get("type"), "general")
        self.assertEqual(g_step.get("app"), "com.a")  # kept as launch hint
        self.assertNotIn("capability", g_step)
        self.assertEqual(runner._step_outcomes[-1]["recovered_via"], "general_fallback")

    def test_both_fallbacks_off_exhausts_ladder(self) -> None:
        runner = _runner(self._tmp)
        rec = runner._recovery
        rec.reroute = MagicMock(return_value=None)
        runner._execute_app_leg = MagicMock(side_effect=[_hard_fail(), _hard_fail()])
        runner._run_mobileworld_step = MagicMock()
        runner._run_general_step = MagicMock()
        with mock.patch("agents.flow.flow_runner.mw_fallback_enabled", return_value=False), \
             mock.patch("agents.flow.flow_runner.general_fallback_enabled", return_value=False), \
             self.assertRaises(RuntimeError):
            runner._run_app_step(dict(_STEP))
        runner._run_mobileworld_step.assert_not_called()
        runner._run_general_step.assert_not_called()

    def test_budget_zero_fails_fast(self) -> None:
        os.environ["RELAY_RECOVERY_MAX_LEGS"] = "0"
        runner = _runner(self._tmp)
        runner._execute_app_leg = MagicMock(side_effect=[_hard_fail()])
        with self.assertRaises(RuntimeError):
            runner._run_app_step(dict(_STEP))
        runner._execute_app_leg.assert_called_once()

    def test_recovery_disabled_restores_fail_fast(self) -> None:
        os.environ["RELAY_RECOVERY"] = "0"
        runner = _runner(self._tmp)
        runner._execute_app_leg = MagicMock(return_value=_hard_fail())
        with self.assertRaises(RuntimeError):
            runner._run_app_step(dict(_STEP))
        runner._execute_app_leg.assert_called_once()  # no extra attempts

    def test_recovery_disabled_keeps_judge_failure_advisory(self) -> None:
        os.environ["RELAY_RECOVERY"] = "0"
        runner = _runner(self._tmp)
        runner._execute_app_leg = MagicMock(return_value=_judged_fail())
        runner._run_app_step(dict(_STEP))  # must not raise
        self.assertEqual(runner.bb["out"], "wrong stuff")


if __name__ == "__main__":
    unittest.main()
