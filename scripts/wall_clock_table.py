#!/usr/bin/env python3
"""Emit a per-task paired wall-clock table (mw vs relay) from normalized results.

Reads ``results_normalized.jsonl`` (produced by ``normalize_wall_clock.py``, which
adds ``elapsed_s_raw`` and ``elapsed_s_norm``) and writes one row per task with
both systems side by side:

    task | category | apps | mw_raw_s mw_norm_s mw_ok | ra_raw_s ra_norm_s ra_ok

where ``*_raw`` is the measured subprocess wall-clock, ``*_norm`` the
queue-noise-normalized wall-clock, and ``ok`` the uniform VLM judge verdict
(``verdict.status == "success"``). Missing side → blank cells.

Usage:
    scripts/wall_clock_table.py traj_logs/phaseB/*/results_normalized.jsonl \
        --out traj_logs/phaseB/wall_clock_table.csv
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def _ok(row: dict[str, Any]) -> bool | None:
    """Success per the uniform judge; None if no verdict was recorded."""
    v = row.get("verdict")
    if not isinstance(v, dict) or v.get("status") is None:
        return None
    return v.get("status") == "success"


def _raw(row: dict[str, Any]) -> float | None:
    v = row.get("elapsed_s_raw", row.get("elapsed_s"))
    return None if v is None else float(v)


def _norm(row: dict[str, Any]) -> float | None:
    v = row.get("elapsed_s_norm")
    return None if v is None else float(v)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", type=Path,
                    help="one or more results_normalized.jsonl")
    ap.add_argument("--out", type=Path, default=Path("traj_logs/phaseB/wall_clock_table.csv"))
    args = ap.parse_args()

    # task id -> {system -> row}; keep first-seen task metadata.
    tasks: dict[str, dict[str, Any]] = {}
    meta: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for f in args.results:
        if not f.exists():
            print(f"[warn] skip missing {f}")
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            tid = r.get("id")
            if tid not in tasks:
                tasks[tid] = {}
                order.append(tid)
                apps = r.get("apps") or ([r.get("app")] if r.get("app") else [])
                meta[tid] = {"category": r.get("category") or "",
                             "apps": "|".join(a for a in apps if a)}
            tasks[tid][r.get("system")] = r

    def cell(row, fn, fmt=None):
        if row is None:
            return ""
        v = fn(row)
        if v is None:
            return ""
        return fmt(v) if fmt else v

    def ok_cell(row):
        if row is None:
            return ""
        o = _ok(row)
        return "" if o is None else int(o)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    header = ["task", "category", "apps",
              "mw_raw_s", "mw_norm_s", "mw_success",
              "ra_raw_s", "ra_norm_s", "ra_success"]
    n_both = 0
    with args.out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for tid in order:
            mw = tasks[tid].get("mw")
            ra = tasks[tid].get("relay")
            if mw is not None and ra is not None:
                n_both += 1
            w.writerow([
                tid, meta[tid]["category"], meta[tid]["apps"],
                cell(mw, _raw, lambda x: f"{x:.1f}"), cell(mw, _norm, lambda x: f"{x:.1f}"),
                ok_cell(mw),
                cell(ra, _raw, lambda x: f"{x:.1f}"), cell(ra, _norm, lambda x: f"{x:.1f}"),
                ok_cell(ra),
            ])

    # Pretty stdout view (✓/✗ for readability).
    def tick(row):
        o = _ok(row) if row else None
        return "-" if o is None else ("✓" if o else "✗")

    print(f"{'task':34s} {'mw_raw':>7s} {'mw_norm':>7s} {'mw':>2s}   "
          f"{'ra_raw':>7s} {'ra_norm':>7s} {'ra':>2s}")
    for tid in order:
        mw, ra = tasks[tid].get("mw"), tasks[tid].get("relay")
        mr, mn = _raw(mw) if mw else None, _norm(mw) if mw else None
        rr, rn = _raw(ra) if ra else None, _norm(ra) if ra else None
        print(f"{tid[:34]:34s} "
              f"{(f'{mr:.1f}' if mr is not None else '-'):>7s} "
              f"{(f'{mn:.1f}' if mn is not None else '-'):>7s} {tick(mw):>2s}   "
              f"{(f'{rr:.1f}' if rr is not None else '-'):>7s} "
              f"{(f'{rn:.1f}' if rn is not None else '-'):>7s} {tick(ra):>2s}")

    print(f"\n{len(order)} tasks ({n_both} with both systems) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
