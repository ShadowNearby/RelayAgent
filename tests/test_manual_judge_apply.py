"""Unit tests for manual_judge's summary recomputation.

results.jsonl is append-only and PhaseB re-runs cases into the same file, so a
(id, system) cell can have several lines. `apply` must aggregate the LAST line
per cell — same convention as phaseB_summary / plot_eval_figs — or a rerun task
is counted twice (old failure + new success).
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for p in (str(_REPO), str(_REPO / "scripts"), str(_REPO / "scripts" / "eval")):
    if p not in sys.path:
        sys.path.insert(0, p)

mj = importlib.import_module("manual_judge")
rbt = importlib.import_module("run_benchmark_test")


def _row(tid: str, system: str, status: str, elapsed: float = 10.0) -> dict:
    return {"id": tid, "system": system, "verdict": {"status": status},
            "elapsed_s": elapsed, "total_tokens": 100}


class LatestCellsTests(unittest.TestCase):
    def test_last_line_per_cell_wins(self) -> None:
        rows = [_row("t1", "relay", "failure"),
                _row("t1", "relay", "success"),
                _row("t1", "mw", "failure"),
                _row("t2", "relay", "success")]
        latest = mj._latest_cells(rows)
        self.assertEqual(len(latest), 3)
        self.assertEqual(
            {(r["id"], r["system"]): r["verdict"]["status"] for r in latest},
            {("t1", "relay"): "success", ("t1", "mw"): "failure",
             ("t2", "relay"): "success"})

    def test_no_duplicates_is_identity(self) -> None:
        rows = [_row("t1", "relay", "success"), _row("t2", "relay", "failure")]
        self.assertEqual(mj._latest_cells(rows), rows)

    def test_aggregate_is_not_double_counted(self) -> None:
        rows = [_row("t1", "relay", "failure"),
                _row("t1", "relay", "success"),
                _row("t2", "relay", "success")]
        raw = rbt._aggregate(rows, ["relay"])["relay"]
        deduped = rbt._aggregate(mj._latest_cells(rows), ["relay"])["relay"]
        self.assertEqual((raw["total"], raw["success"], raw["failure"]), (3, 2, 1))
        self.assertEqual((deduped["total"], deduped["success"], deduped["failure"]),
                         (2, 2, 0))
        self.assertEqual(deduped["completion_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
