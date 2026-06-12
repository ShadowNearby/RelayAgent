"""Tests for agents.nl_flow — the shared NL→flow pipeline core.

Covers the plan_request outcome states (fresh / cached / unsatisfiable /
validation-failed / MW-legs-disallowed) and the cache + persist helpers,
with a stub planner so no LLM or device is touched.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from agents.flow_planner import PlanValidationError
from agents.nl_flow import (
    PlanResult,
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

    def plan(self, nl):
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

    def test_cached_reroute_validation_failure(self):
        persist_plan(_PLAN, "find a bookstore", self.tmp)
        exc = PlanValidationError("nl", _PLAN, ["route gone"])
        res = self._request(planner=_StubPlanner(plan=_PLAN, validate_exc=exc))
        self.assertFalse(res.ok)
        self.assertIs(res.validation, exc)
        self.assertTrue(res.from_cache)

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


if __name__ == "__main__":
    unittest.main()
