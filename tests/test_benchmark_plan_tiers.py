"""Unit tests for the benchmark driver's plan-only tier classification.

Pins the pure dict-in/dict-out functions that produce the paper's coverage
numbers: leg-kind classification (including the manifest-free `general`
fallback leg), the four whole-plan tiers, the plan_summary `mw_fallback` block,
the executed-leg kind harvested off disk, and the small A/B-symmetry helpers
(mw timeout, normalization-constant loading).
"""
from __future__ import annotations

import importlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for p in (str(_REPO), str(_REPO / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

rbt = importlib.import_module("run_benchmark_test")


def _spec(app: str = "com.autonavi.minimap", cap: str = "navigate_to") -> dict:
    return {"id": "go", "type": "app", "app": app, "capability": cap}


class LegKindTests(unittest.TestCase):
    def test_ask_user_is_not_a_leg(self) -> None:
        self.assertIsNone(rbt._leg_kind({"id": "confirm", "type": "ask_user"}))

    def test_non_dict_is_not_a_leg(self) -> None:
        self.assertIsNone(rbt._leg_kind("nope"))

    def test_specialized_and_foundation(self) -> None:
        self.assertEqual(rbt._leg_kind(_spec()), "specialized")
        self.assertEqual(
            rbt._leg_kind(_spec("com.aliyun.tongyi", "foundation_llm")), "foundation")

    def test_mw_leg_keeps_its_step_id(self) -> None:
        # a per-leg conversion pops `capability` but keeps id AND app hint
        step = {"id": "check_calendar", "type": "mobileworld", "app": "com.a"}
        self.assertEqual(rbt._leg_kind(step), "mw")

    def test_general_leg_is_not_specialized(self) -> None:
        # the general fallback leg has no capability but DOES keep an app hint —
        # the exact shape that used to be misread as a specialized route
        step = {"id": "check_calendar", "type": "general", "app": "com.a"}
        self.assertEqual(rbt._leg_kind(step), "general")
        self.assertEqual(rbt._leg_kind({"id": "x", "type": "general"}), "general")


class PlanTierTests(unittest.TestCase):
    def _legs(self, *kinds: str) -> list[dict[str, str]]:
        return [{"app": "com.a", "capability": "c", "kind": k} for k in kinds]

    def test_four_tiers(self) -> None:
        self.assertEqual(rbt._plan_tier(self._legs("specialized", "specialized")), "covered")
        self.assertEqual(rbt._plan_tier(self._legs("specialized", "foundation")),
                         "foundation_fallback")
        self.assertEqual(rbt._plan_tier(self._legs("mw", "mw")), "mw")
        self.assertEqual(rbt._plan_tier(self._legs("mw", "specialized")), "mixed")

    def test_general_legs_are_fallback_not_covered(self) -> None:
        self.assertEqual(rbt._plan_tier(self._legs("general")), "mw")
        self.assertEqual(rbt._plan_tier(self._legs("general", "specialized")), "mixed")
        self.assertEqual(rbt._plan_tier(self._legs("general", "mw")), "mw")

    def test_no_legs_is_foundation_fallback(self) -> None:
        self.assertEqual(rbt._plan_tier([]), "foundation_fallback")


class PlanLegStatsTests(unittest.TestCase):
    def test_stats_of_a_mixed_plan(self) -> None:
        plan = {"steps": [
            _spec("com.autonavi.minimap", "navigate_to"),
            {"id": "ask", "type": "ask_user"},
            {"id": "check", "type": "mobileworld", "app": "com.b"},
            {"id": "browse", "type": "general", "app": "com.c"},
        ]}
        stats = rbt._plan_leg_stats(rbt._plan_legs(plan))
        self.assertEqual(stats["tier"], "mixed")
        self.assertEqual(stats["n_legs"], 3)          # ask_user is control flow
        self.assertEqual(stats["n_mw_legs"], 2)       # mw + general
        self.assertEqual(stats["mw_ratio"], 0.667)
        self.assertEqual(stats["spec_apps"], ["com.autonavi.minimap"])
        self.assertEqual(stats["spec_caps"], ["navigate_to"])
        self.assertEqual(stats["ra_apps"], ["com.autonavi.minimap", "com.b", "com.c"])
        self.assertEqual([l["kind"] for l in stats["legs"]],
                         ["specialized", "mw", "general"])

    def test_covered_plan(self) -> None:
        stats = rbt._plan_leg_stats(rbt._plan_legs({"steps": [_spec(), _spec("com.b", "x")]}))
        self.assertEqual(stats["tier"], "covered")
        self.assertEqual(stats["n_mw_legs"], 0)
        self.assertEqual(stats["mw_ratio"], 0.0)


class PlanOnlyAggregateTests(unittest.TestCase):
    def _rows(self) -> list[dict]:
        return [
            {"id": "t1", "status": "planned", **rbt._plan_leg_stats(
                rbt._plan_legs({"steps": [_spec(), _spec("com.b", "x")]}))},
            {"id": "t2", "status": "planned", **rbt._plan_leg_stats(
                rbt._plan_legs({"steps": [_spec("com.a", "foundation_llm")]}))},
            {"id": "t3", "status": "planned", **rbt._plan_leg_stats(
                rbt._plan_legs({"steps": [{"id": "a", "type": "mobileworld"},
                                          {"id": "b", "type": "mobileworld"}]}))},
            {"id": "t4", "status": "planned", **rbt._plan_leg_stats(
                rbt._plan_legs({"steps": [_spec(), {"id": "b", "type": "mobileworld"}]}))},
            {"id": "t5", "status": "unsatisfiable", "tier": "mw",
             "n_legs": 0, "legs": [], "n_mw_legs": 0, "mw_ratio": 1.0},
        ]

    def test_by_tier_and_mw_fallback_block(self) -> None:
        agg = rbt._plan_only_aggregate(self._rows(), "report.jsonl")
        self.assertEqual(agg["n_tasks"], 5)
        self.assertEqual(agg["by_tier"],
                         {"mw": 2, "covered": 1, "foundation_fallback": 1, "mixed": 1})
        self.assertEqual(agg["covered_rate"], 0.2)
        mwf = agg["mw_fallback"]
        self.assertEqual(mwf["tasks_fully_mw"], 2)
        self.assertEqual(mwf["tasks_mixed"], 1)
        self.assertEqual(mwf["tasks_touching_mw"], 3)
        self.assertEqual(mwf["task_touch_rate"], 0.6)
        self.assertEqual(mwf["total_legs"], 7)      # 2 + 1 + 2 + 2 + 0
        self.assertEqual(mwf["total_mw_legs"], 3)   # t3 x2 + t4 x1
        self.assertEqual(mwf["mw_leg_rate"], round(3 / 7, 3))
        self.assertEqual(mwf["mixed_task_mw_ratios"], {"t4": 0.5})
        # per-task set membership: t1 (minimap + com.b) and t4 (minimap)
        self.assertEqual(agg["covered_app_hits"],
                         {"com.autonavi.minimap": 2, "com.b": 1})

    def test_general_leg_task_lands_in_the_fallback_stats(self) -> None:
        rows = [{"id": "g1", "status": "planned", **rbt._plan_leg_stats(
            rbt._plan_legs({"steps": [_spec(), {"id": "b", "type": "general",
                                                "app": "com.b"}]}))}]
        agg = rbt._plan_only_aggregate(rows, "report.jsonl")
        self.assertEqual(agg["by_tier"], {"mixed": 1})
        self.assertEqual(agg["covered_rate"], 0.0)
        self.assertEqual(agg["mw_fallback"]["task_touch_rate"], 1.0)
        self.assertEqual(agg["mw_fallback"]["mw_leg_rate"], 0.5)
        self.assertEqual(agg["mw_fallback"]["mixed_task_mw_ratios"], {"g1": 0.5})

    def test_empty_rows(self) -> None:
        agg = rbt._plan_only_aggregate([], "report.jsonl")
        self.assertEqual(agg["n_tasks"], 0)
        self.assertEqual(agg["mw_fallback"]["mw_leg_rate"], 0.0)


class RanLegKindTests(unittest.TestCase):
    """The executed-leg kind, read off a leg dir's own artifacts."""

    def test_mw_leg_is_stamped_in_summary(self) -> None:
        self.assertEqual(
            rbt._ran_leg_kind({"via": "mobileworld"},
                              {"step": "check_calendar", "capability": "fallback"}), "mw")

    def test_general_leg_judged_as_fallback(self) -> None:
        self.assertEqual(
            rbt._ran_leg_kind({"last_action_type": "finish"},
                              {"step": "check_calendar", "capability": "fallback"}), "general")

    def test_app_legs(self) -> None:
        self.assertEqual(rbt._ran_leg_kind({}, {"capability": "navigate_to"}), "specialized")
        self.assertEqual(rbt._ran_leg_kind({}, {"capability": "foundation_llm"}), "foundation")


class HarvestLegKindTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _leg(self, name: str, summary: dict, verdict: dict) -> None:
        d = self._tmp / name
        d.mkdir(parents=True)
        (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        (d / "leg_verdict.json").write_text(json.dumps(verdict), encoding="utf-8")

    def test_per_leg_mw_conversion_keeps_its_id_but_is_tagged_mw(self) -> None:
        self._leg("01_find_place", {"token_usage": {"total_tokens": 10}},
                  {"step": "find_place", "capability": "find_nearby", "status": "success"})
        self._leg("02_check_calendar", {"via": "mobileworld"},
                  {"step": "check_calendar", "capability": "fallback", "status": "success"})
        harvested = rbt._harvest_relay_legs(self._tmp)
        self.assertEqual([l["kind"] for l in harvested["legs"]], ["specialized", "mw"])
        self.assertEqual([l["step"] for l in harvested["legs"]],
                         ["find_place", "check_calendar"])


class MwTimeoutTests(unittest.TestCase):
    def test_explicit_timeout_forwarded(self) -> None:
        self.assertEqual(rbt._mw_timeout_arg(900.0), "900")

    def test_disabled_is_not_silently_600(self) -> None:
        # --task-timeout 0 -> relay runs unbounded; mw must not be capped
        self.assertEqual(rbt._mw_timeout_arg(None), str(rbt.MW_TIMEOUT_DISABLED))
        self.assertGreater(rbt.MW_TIMEOUT_DISABLED, 10 ** 6)


class LoadNormConstTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, payload) -> Path:
        p = self._tmp / "fit.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return p

    def test_good_fit(self) -> None:
        p = self._write({"gamma_s_per_call": 0.5, "alpha_s_per_prefill_tok": 1e-4,
                         "beta_s_per_decode_tok": 2e-3})
        self.assertEqual(rbt._load_norm_const(p), (0.5, 1e-4, 2e-3))

    def test_missing_file(self) -> None:
        self.assertIsNone(rbt._load_norm_const(self._tmp / "nope.json"))

    def test_null_constant_does_not_raise(self) -> None:
        p = self._write({"gamma_s_per_call": 0.5, "alpha_s_per_prefill_tok": None,
                         "beta_s_per_decode_tok": 2e-3})
        self.assertIsNone(rbt._load_norm_const(p))

    def test_json_array_does_not_raise(self) -> None:
        self.assertIsNone(rbt._load_norm_const(self._write([1, 2, 3])))

    def test_garbage_text_does_not_raise(self) -> None:
        p = self._tmp / "fit.json"
        p.write_text("{not json", encoding="utf-8")
        self.assertIsNone(rbt._load_norm_const(p))


if __name__ == "__main__":
    unittest.main()
