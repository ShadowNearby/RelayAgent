"""Unit tests for the benchmark driver's recovery telemetry (roadmap P1-R4).

Pins the flow_report.json → per-row `recovery` harvest and the summary-level
first-try vs final success / per-tier hit-rate aggregation, without a device.
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


def _row(status: str, recovery: dict | None) -> dict:
    return {"system": "relay", "verdict": {"status": status}, "recovery": recovery}


def _rec(attempts: list[dict], recovered_steps: int = 0, tokens_used: int = 0) -> dict:
    return {"enabled": True, "attempts": attempts,
            "recovered_steps": recovered_steps, "tokens_used": tokens_used}


class HarvestRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_missing_report_returns_none(self) -> None:
        self.assertIsNone(rbt._harvest_recovery(self._tmp))

    def test_reads_flow_report(self) -> None:
        (self._tmp / "flow_report.json").write_text(json.dumps({
            "steps": [
                {"step": "s1", "status": "ok"},
                {"step": "s2", "status": "recovered", "recovered_via": "retry"},
            ],
            "recovery": {
                "enabled": True, "extra_legs_used": 1, "tokens_used": 800,
                "attempts": [{"step": "s2", "tier": "retry", "target": "com.a/cap",
                              "outcome": "ok", "detail": "", "tokens": 800}],
            },
        }), encoding="utf-8")
        rec = rbt._harvest_recovery(self._tmp)
        self.assertTrue(rec["enabled"])
        self.assertFalse(rec["first_try_clean"])
        self.assertEqual(rec["recovered_steps"], 1)
        self.assertEqual(rec["extra_legs_used"], 1)
        self.assertEqual(len(rec["attempts"]), 1)

    def test_clean_run_is_first_try(self) -> None:
        (self._tmp / "flow_report.json").write_text(json.dumps({
            "steps": [{"step": "s1", "status": "ok"}],
            "recovery": {"enabled": True, "extra_legs_used": 0,
                         "tokens_used": 0, "attempts": []},
        }), encoding="utf-8")
        rec = rbt._harvest_recovery(self._tmp)
        self.assertTrue(rec["first_try_clean"])
        self.assertEqual(rec["recovered_steps"], 0)


class AggregateRecoveryTests(unittest.TestCase):
    def test_none_when_ladder_never_on(self) -> None:
        rows = [_row("success", None),
                _row("failure", {"enabled": False, "attempts": []})]
        self.assertIsNone(rbt._aggregate_recovery(rows))

    def test_first_try_vs_final_and_tiers(self) -> None:
        rows = [
            # clean first-try success
            _row("success", _rec([])),
            # flipped by the ladder: retry failed, MW fallback hit
            _row("success", _rec(
                [{"tier": "retry", "outcome": "failed", "tokens": 800},
                 {"tier": "mw_fallback", "outcome": "ok", "tokens": 0}],
                recovered_steps=1, tokens_used=5500)),
            # ladder fired (reroute skipped — no alternative) but stayed failed
            _row("failure", _rec(
                [{"tier": "reroute", "outcome": "skipped", "tokens": 0}])),
            # a row without telemetry (pre-P1 flow) is excluded from n
            _row("failure", None),
        ]
        agg = rbt._aggregate_recovery(rows)
        self.assertEqual(agg["n"], 3)
        self.assertEqual(agg["first_try_success"], 1)
        self.assertEqual(agg["final_success"], 2)
        self.assertEqual(agg["ladder_fired"], 2)
        self.assertEqual(agg["recovered_tasks"], 1)
        self.assertEqual(agg["by_tier"]["retry"], {"tried": 1, "ok": 0, "tokens": 800})
        self.assertEqual(agg["by_tier"]["mw_fallback"], {"tried": 1, "ok": 1, "tokens": 0})
        # a skipped attempt is not a try (the tier never actually ran)
        self.assertEqual(agg["by_tier"]["reroute"]["tried"], 0)
        self.assertEqual(agg["recovery_tokens"], {"total": 5500, "mean_when_fired": 2750})


if __name__ == "__main__":
    unittest.main()
