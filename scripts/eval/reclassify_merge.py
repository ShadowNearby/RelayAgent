#!/usr/bin/env python3
"""Merge plan-only reclassification: old `covered` rows (kept, not re-run) + a
fresh rerun of the rest → one combined plan_report.jsonl + plan_summary.json per
benchmark, using the current leg-kind tier logic.

Covered tasks are stable under the new logic (covered = every leg specialized;
the stage-3 escape only downgrades foundation-fallback steps), so old covered rows
are carried verbatim and only the non-covered set is re-run. AndroidDaily's old
report was lost, so it is a full rerun (no covered rows to merge).

Run AFTER the reruns finish:  uv run python scripts/eval/reclassify_merge.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from run_benchmark_test import _plan_only_aggregate  # noqa: E402

RECLASS = REPO / "traj_logs" / "reclassify"
OUT = RECLASS / "final"

# benchmark -> (covered_rows_file_or_None, rerun_report)
SPEC = {
    "relaybench":   (RECLASS / "relaybench_covered_rows.jsonl",  RECLASS / "relaybench_rerun" / "plan_report.jsonl"),
    "mobileworld":  (RECLASS / "mobileworld_covered_rows.jsonl", RECLASS / "mobileworld_rerun" / "plan_report.jsonl"),
    "androiddaily": (None,                                       RECLASS / "androiddaily_rerun" / "plan_report.jsonl"),
}


def _load(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _dedupe_rerun_wins(rows: list[dict]) -> list[dict]:
    """De-dupe by id, LAST occurrence wins: rerun rows are appended after the
    old covered rows, and plan_report.jsonl itself is append-mode ("a"), so
    within one file a later line is the fresher run too. Each id keeps its
    first-seen position (deterministic order) but carries the latest row."""
    latest: dict[str, dict] = {}
    for r in rows:
        latest[r["id"]] = r
    return list(latest.values())


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    overall = {}
    for bench, (cov_file, rerun_rep) in SPEC.items():
        rows = _load(rerun_rep)
        if cov_file is not None:
            rows = _load(cov_file) + rows
        # de-dupe by id (rerun wins if any overlap), keep deterministic order
        merged = _dedupe_rerun_wins(rows)
        report = OUT / f"{bench}_plan_report.jsonl"
        with report.open("w", encoding="utf-8") as fh:
            for r in merged:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        summary = _plan_only_aggregate(merged, report)
        (OUT / f"{bench}_plan_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        overall[bench] = {"n": summary["n_tasks"], "by_tier": summary["by_tier"],
                          "covered_rate": summary["covered_rate"],
                          "mw_task_touch_rate": summary["mw_fallback"]["task_touch_rate"],
                          "mw_leg_rate": summary["mw_fallback"]["mw_leg_rate"]}
        print(f"\n=== {bench} (n={summary['n_tasks']}) ===")
        print("  by_tier:", summary["by_tier"])
        print("  covered_rate:", summary["covered_rate"],
              " mw_task_touch:", summary["mw_fallback"]["task_touch_rate"],
              " mw_leg_rate:", summary["mw_fallback"]["mw_leg_rate"])
        # covered id list for Phase B real-device A/B
        cov_ids = [r["id"] for r in merged if r.get("tier") == "covered"]
        (OUT / f"{bench}_covered_ids.txt").write_text("\n".join(cov_ids) + "\n", encoding="utf-8")
        print(f"  covered ids -> {OUT / f'{bench}_covered_ids.txt'} ({len(cov_ids)})")
    (OUT / "overview.json").write_text(json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\noverview:", json.dumps(overall, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
