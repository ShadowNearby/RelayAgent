"""Tests for agents.flow.nl_flow — the shared NL→flow pipeline core.

Covers the plan_request outcome states (fresh / cached / unsatisfiable /
validation-failed / MW-legs-disallowed), the cache + persist helpers, and the
P3-M3 propose-then-ask memory gate, with a stub planner so no LLM or device is
touched.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

import yaml

from agents.flow.flow_planner import PlanValidationError
from agents.flow.nl_flow import (
    PlanResult,
    _maybe_remember_preference,
    cache_lookup,
    normalize_request,
    persist_plan,
    plan_has_mw_legs,
    plan_packages,
    plan_request,
)

_PLAN = {
    "flow_id": "f1",
    "description": "demo",
    "apps_required": [{"app_id": "com.example.a", "use_capability": "qa"}],
    "steps": [
        {"id": "s1", "app": "com.example.a", "capability": "qa",
         "prompt": "ask something", "bind": "answer"},
    ],
}

_MW_PLAN = {
    **_PLAN,
    "steps": _PLAN["steps"] + [
        {"id": "s2", "type": "mobileworld", "prompt": "uncovered leg"},
    ],
}


class _StubPlanner:
    """Programmable FlowPlanner stand-in for the three calls plan_request makes."""

    def __init__(self, plan=None, plan_exc=None, validate_exc=None):
        self._plan = plan
        self._plan_exc = plan_exc
        self._validate_exc = validate_exc
        self.resolved = 0
        self.validated = 0
        self.planned = 0

    def plan(self, nl):
        self.planned += 1
        if self._plan_exc:
            raise self._plan_exc
        return dict(self._plan)

    def resolve_app_routes(self, plan, nl):
        self.resolved += 1
        return plan

    def validate_plan(self, plan, nl):
        self.validated += 1
        if self._validate_exc:
            raise self._validate_exc


class CacheHelpersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_persist_then_lookup_with_whitespace_variation(self):
        req = "帮我 找一家  书店"
        path = persist_plan(_PLAN, req, self.tmp)
        self.assertTrue(path.exists())
        hit = cache_lookup("帮我 找一家 书店", self.tmp)  # normalized match
        self.assertEqual(hit, path)
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(doc["source_request"], normalize_request(req))
        self.assertEqual(doc["steps"], _PLAN["steps"])

    def test_lookup_miss(self):
        persist_plan(_PLAN, "request one", self.tmp)
        self.assertIsNone(cache_lookup("different request", self.tmp))

    def test_plan_packages_dedupe_in_order(self):
        plan = {"steps": [
            {"app": "b"}, {"app": "a"}, {"app": "b"}, {"type": "ask_user"},
        ]}
        self.assertEqual(plan_packages(plan), ["b", "a"])

    def test_plan_has_mw_legs(self):
        self.assertFalse(plan_has_mw_legs(_PLAN))
        self.assertTrue(plan_has_mw_legs(_MW_PLAN))


class PlanRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _request(self, nl="find a bookstore", **kwargs) -> PlanResult:
        kwargs.setdefault("generated_dir", self.tmp)
        return plan_request(nl, **kwargs)

    def test_fresh_plan_ok(self):
        res = self._request(planner=_StubPlanner(plan=_PLAN))
        self.assertTrue(res.ok)
        self.assertFalse(res.from_cache)
        self.assertTrue(res.plan_path.exists())

    def test_fresh_plan_unsatisfiable(self):
        res = self._request(
            planner=_StubPlanner(plan={"unsatisfiable": True, "reason": "no app"})
        )
        self.assertFalse(res.ok)
        self.assertTrue(res.unsatisfiable)
        self.assertEqual(res.reason, "no app")
        self.assertIsNone(res.plan_path)

    def test_fresh_plan_validation_error(self):
        exc = PlanValidationError("nl", {"steps": []}, ["bad step"])
        res = self._request(planner=_StubPlanner(plan_exc=exc))
        self.assertFalse(res.ok)
        self.assertIs(res.validation, exc)
        self.assertFalse(res.from_cache)

    def test_cache_hit_revalidates_and_marks_cached(self):
        persist_plan(_PLAN, "find a bookstore", self.tmp)
        planner = _StubPlanner(plan=_PLAN)
        res = self._request(planner=planner)
        self.assertTrue(res.ok)
        self.assertTrue(res.from_cache)
        self.assertEqual(planner.resolved, 1)
        self.assertEqual(planner.validated, 1)

    def test_cache_disabled_synthesizes(self):
        persist_plan(_PLAN, "find a bookstore", self.tmp)
        planner = _StubPlanner(plan=_PLAN)
        res = self._request(planner=planner, use_cache=False)
        self.assertTrue(res.ok)
        self.assertFalse(res.from_cache)
        self.assertEqual(planner.resolved, 0)

    def test_cached_reroute_failure_falls_back_to_fresh_synthesis(self):
        # A cached plan can stop routing when the capability matrix or the
        # platform card set changes. Per CLAUDE.md (MW > general >
        # unsatisfiable) that must not abort: re-synthesize, which routes the
        # coverage gap through the fallbacks. `--no-cache` already worked; the
        # cached path must not be the asymmetric loser.
        persist_plan(_PLAN, "find a bookstore", self.tmp)
        exc = PlanValidationError("nl", _PLAN, ["route gone"])
        planner = _StubPlanner(plan=_PLAN, validate_exc=exc)
        res = self._request(planner=planner)
        self.assertTrue(res.ok)
        self.assertFalse(res.from_cache)  # served by fresh synthesis
        self.assertEqual(planner.planned, 1)

    def test_cached_reroute_failure_then_fresh_failure_surfaces_validation(self):
        # Fresh synthesis is the last word: if it fails too, that error is
        # what the caller sees.
        persist_plan(_PLAN, "find a bookstore", self.tmp)
        cached_exc = PlanValidationError("nl", _PLAN, ["route gone"])
        fresh_exc = PlanValidationError("nl", _PLAN, ["still broken"])
        planner = _StubPlanner(
            plan=_PLAN, plan_exc=fresh_exc, validate_exc=cached_exc
        )
        res = self._request(planner=planner)
        self.assertFalse(res.ok)
        self.assertIs(res.validation, fresh_exc)
        self.assertEqual(planner.planned, 1)

    def test_cached_reroute_failure_can_end_unsatisfiable(self):
        persist_plan(_PLAN, "find a bookstore", self.tmp)
        exc = PlanValidationError("nl", _PLAN, ["route gone"])
        planner = _StubPlanner(
            plan={"unsatisfiable": True, "reason": "no app"}, validate_exc=exc
        )
        res = self._request(planner=planner)
        self.assertTrue(res.unsatisfiable)
        self.assertEqual(res.reason, "no app")

    def test_cached_unsatisfiable_marker(self):
        path = self.tmp / "unsat.yaml"
        path.write_text(
            yaml.safe_dump({
                "source_request": "find a bookstore",
                "unsatisfiable": True, "reason": "nothing covers it",
            }, allow_unicode=True),
            encoding="utf-8",
        )
        res = self._request(planner=_StubPlanner(plan=_PLAN))
        self.assertTrue(res.unsatisfiable)
        self.assertEqual(res.reason, "nothing covers it")

    def test_cached_mw_legs_disallowed(self):
        persist_plan(_MW_PLAN, "find a bookstore", self.tmp)
        res = self._request(
            planner=_StubPlanner(plan=_MW_PLAN), allow_mw_legs=False
        )
        self.assertTrue(res.unsatisfiable)
        self.assertIn("MobileWorld", res.reason)

    def test_cached_mw_legs_allowed_by_default(self):
        persist_plan(_MW_PLAN, "find a bookstore", self.tmp)
        res = self._request(planner=_StubPlanner(plan=_MW_PLAN))
        self.assertTrue(res.ok)  # host behavior unchanged: cached MW plans run


class MaybeRememberPreferenceTests(unittest.TestCase):
    """P3-M3: the propose-then-ask gate. Nothing reaches the profile without an
    explicit affirmative answer — an EOF'd stdin (batch runs), a declined
    answer, a proposal failure and an LLM/IO error all mean "don't write"."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["RELAY_PROFILE_ROOT"] = str(self.tmp)
        os.environ.pop("RELAY_PROFILE", None)

    def tearDown(self):
        for k in ("RELAY_PROFILE_ROOT", "RELAY_PROFILE"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def _store(self) -> Path:
        return self.tmp / "profile.yaml"

    def _runner(self) -> SimpleNamespace:
        return SimpleNamespace(_llm=MagicMock(), env={"LLM_MODEL": "qwen"})

    def _run(self, answer, proposal=("milk_tea", "去冰")):
        """Drive the gate with a scripted proposal + user answer; returns the
        ask_user prompt text (or None when the user was never asked)."""
        with mock.patch(
            "agents.flow.user_profile.propose_memory", return_value=proposal
        ), mock.patch("agents.runtime.interaction.get_interaction") as gi:
            gi.return_value.ask_user.return_value = answer
            _maybe_remember_preference(self._runner(), "点奶茶去冰", {"o": "ok"})
            calls = gi.return_value.ask_user.call_args_list
            return calls[0][0][0] if calls else None

    def _saved(self) -> dict:
        if not self._store.exists():
            return {}
        return yaml.safe_load(self._store.read_text(encoding="utf-8")) or {}

    def test_affirmative_answer_writes_the_preference(self):
        for answer in ("y", "yes", "是", " Y "):
            with self.subTest(answer=answer):
                self._store.unlink(missing_ok=True)
                self._run(answer)
                self.assertEqual(
                    self._saved().get("preferences"), {"milk_tea": "去冰"}
                )

    def test_eof_declines_and_writes_nothing(self):
        # get_interaction().ask_user returns None on EOF (batch stdin) — the
        # never-write-silently rule makes that a refusal.
        self._run(None)
        self.assertFalse(self._store.exists())

    def test_negative_and_empty_answers_write_nothing(self):
        for answer in ("n", "no", "", "  ", "maybe", "不"):
            with self.subTest(answer=answer):
                self._run(answer)
                self.assertFalse(self._store.exists())

    def test_user_is_asked_with_the_proposed_key_and_value(self):
        prompt = self._run("n")
        self.assertIn("milk_tea", prompt)
        self.assertIn("去冰", prompt)

    def test_no_proposal_never_asks(self):
        self.assertIsNone(self._run("y", proposal=None))
        self.assertFalse(self._store.exists())

    def test_profile_switch_off_skips_the_pass_entirely(self):
        os.environ["RELAY_PROFILE"] = "0"
        with mock.patch(
            "agents.flow.user_profile.propose_memory", return_value=("k", "v")
        ) as propose:
            _maybe_remember_preference(self._runner(), "req", {})
        propose.assert_not_called()
        self.assertFalse(self._store.exists())

    def test_write_merges_into_an_existing_store(self):
        self._store.write_text(
            yaml.safe_dump({"addresses": {"home": "X路1号"}}, allow_unicode=True),
            encoding="utf-8",
        )
        self._run("y")
        saved = self._saved()
        self.assertEqual(saved["addresses"], {"home": "X路1号"})
        self.assertEqual(saved["preferences"], {"milk_tea": "去冰"})

    def test_errors_never_propagate(self):
        # Memory is best-effort: a failure here must not fail a finished flow.
        with mock.patch(
            "agents.flow.user_profile.propose_memory",
            side_effect=RuntimeError("gateway down"),
        ):
            _maybe_remember_preference(self._runner(), "req", {})  # must not raise
        self.assertFalse(self._store.exists())


if __name__ == "__main__":
    unittest.main()
