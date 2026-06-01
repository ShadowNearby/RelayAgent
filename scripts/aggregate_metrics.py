#!/usr/bin/env python3
"""Aggregate token / time / reply-length metrics from RelayAgent traj logs.

Built for the tech-report A/B benchmark: point it at one or more run dirs
(each containing a traj.json, e.g. test-results/run_all_caps/<RUN_ID>/<slug>/)
and it prints a per-run table plus a baseline-vs-optimized delta if two runs
share the same slug.

Metrics per run (from traj.json `llm_calls`, which carry per-call `purpose`,
`usage_delta`, and `elapsed_s`):
  - total tokens (prompt / completion / cached / total)
  - VLM call count + tokens broken down by purpose
    (capability_router | grounding | reply_watch)
  - total VLM wall-clock (sum of elapsed_s)
  - reply char count (RELAY_REPLY_OUT json if present, else traj last reply)

Wall-clock end-to-end seconds come from the run_all_caps summary.tsv if given.

Usage:
    scripts/aggregate_metrics.py PATH [PATH ...] [--summary summary.tsv] [--json]
    # PATH may be a traj.json, a run dir containing one, or a tree to walk.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PURPOSES = ("capability_router", "grounding", "reply_watch")


def find_traj_files(path: Path) -> list[Path]:
    if path.is_file() and path.name == "traj.json":
        return [path]
    if path.is_dir():
        direct = path / "traj.json"
        if direct.exists():
            return [direct]
        return sorted(path.rglob("traj.json"))
    return []


def wall_seconds(run_dir: Path) -> float | None:
    """Read wall_clock.json dropped by run_test.py / flow_runner (RELAY_TIMING=1)."""
    cand = run_dir / "wall_clock.json"
    if cand.exists():
        try:
            return json.loads(cand.read_text()).get("wall_s")
        except Exception:
            return None
    return None


def reply_chars(run_dir: Path, traj: dict) -> int:
    """Prefer a RELAY_REPLY_OUT dump (verbatim), else the traj's last reply."""
    for cand in (run_dir / "reply.json", run_dir / "reply_out.json"):
        if cand.exists():
            try:
                d = json.loads(cand.read_text())
                txt = d.get("reply") or d.get("text") or ""
                if txt:
                    return len(txt)
            except Exception:
                pass
    # Fallback: scan traj for the longest ask_user / reply-ish string.
    best = 0
    for trial in traj.values():
        for step in trial.get("traj", []) or []:
            resp = step.get("ask_user_response") or ""
            if isinstance(resp, str):
                best = max(best, len(resp))
    return best


def aggregate(traj_path: Path) -> dict:
    traj = json.loads(traj_path.read_text())
    out = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
        "vlm_calls": 0,
        "vlm_elapsed_s": 0.0,
        "by_purpose": {p: {"calls": 0, "tokens": 0} for p in PURPOSES},
        "reply_chars": reply_chars(traj_path.parent, traj),
        "wall_s": wall_seconds(traj_path.parent),
    }
    for trial in traj.values():
        calls = trial.get("llm_calls", []) or []
        if calls:
            for call in calls:
                ud = call.get("usage_delta") or {}
                pt = ud.get("prompt_tokens", 0) or 0
                ct = ud.get("completion_tokens", 0) or 0
                cached = ud.get("cached_tokens", 0) or 0
                tot = pt + ct
                out["prompt_tokens"] += pt
                out["completion_tokens"] += ct
                out["cached_tokens"] += cached
                out["total_tokens"] += tot
                out["vlm_calls"] += 1
                out["vlm_elapsed_s"] += call.get("elapsed_s", 0.0) or 0.0
                purpose = call.get("purpose")
                if purpose in out["by_purpose"]:
                    out["by_purpose"][purpose]["calls"] += 1
                    out["by_purpose"][purpose]["tokens"] += tot
        else:
            # Flow sub-runs (mw test via --log-file-root) write only the
            # aggregate token_usage, not per-call llm_calls. Fall back to it
            # for totals; per-purpose breakdown is unavailable (left at 0).
            tu = trial.get("token_usage") or {}
            out["prompt_tokens"] += tu.get("prompt_tokens", 0) or 0
            out["completion_tokens"] += tu.get("completion_tokens", 0) or 0
            out["cached_tokens"] += tu.get("cached_tokens", 0) or 0
            out["total_tokens"] += tu.get("total_tokens", 0) or 0
            out["llm_calls_unavailable"] = True
    return out


def load_summary_seconds(summary: Path) -> dict[str, float]:
    """Map slug (pkg__cap) -> end-to-end seconds from run_all_caps summary.tsv."""
    secs: dict[str, float] = {}
    if not summary.exists():
        return secs
    lines = summary.read_text().splitlines()
    header = lines[0].split("\t") if lines else []
    try:
        i_pkg, i_cap, i_sec = (
            header.index("pkg"),
            header.index("cap"),
            header.index("seconds"),
        )
    except ValueError:
        return secs
    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) <= max(i_pkg, i_cap, i_sec):
            continue
        try:
            secs[f"{cols[i_pkg]}__{cols[i_cap]}"] = float(cols[i_sec])
        except ValueError:
            pass
    return secs


def fmt_row(label: str, m: dict, seconds: float | None) -> str:
    bp = m["by_purpose"]
    return (
        f"{label:<28} "
        f"tok={m['total_tokens']:>6} "
        f"(p={m['prompt_tokens']} c={m['completion_tokens']} "
        f"cached={m['cached_tokens']}) "
        f"vlm={m['vlm_calls']:>2} "
        f"[router={bp['capability_router']['calls']} "
        f"ground={bp['grounding']['calls']} "
        f"reply={bp['reply_watch']['calls']}] "
        f"vlm_s={m['vlm_elapsed_s']:>5.1f} "
        f"reply_chars={m['reply_chars']:>5} "
        f"wall_s={seconds if seconds is not None else '?'}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--summary", type=Path, help="run_all_caps summary.tsv for wall-clock seconds")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()

    secs = load_summary_seconds(args.summary) if args.summary else {}

    rows: dict[str, dict] = {}
    for p in args.paths:
        for tf in find_traj_files(p):
            label = tf.parent.name  # slug or run dir name
            rows[str(tf.parent)] = {"label": label, "metrics": aggregate(tf)}

    if not rows:
        sys.exit("no traj.json found under given paths")

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print(f"{'run':<28} metrics")
    print("-" * 110)
    for key, r in rows.items():
        label = r["label"]
        m = r["metrics"]
        # Prefer the per-run wall_clock.json (RELAY_TIMING=1) over summary.tsv.
        wall = m["wall_s"] if m["wall_s"] is not None else secs.get(label)
        print(fmt_row(label, m, wall))

    # If exactly two runs and they share a slug-ish label, print a delta.
    if len(rows) == 2:
        (_, a), (_, b) = rows.items()
        ma, mb = a["metrics"], b["metrics"]
        if ma["total_tokens"]:
            d_tok = 100.0 * (mb["total_tokens"] - ma["total_tokens"]) / ma["total_tokens"]
            print("-" * 110)
            print(
                f"Δ tokens {a['label']}→{b['label']}: "
                f"{ma['total_tokens']} → {mb['total_tokens']} ({d_tok:+.1f}%)  "
                f"reply_chars {ma['reply_chars']} → {mb['reply_chars']} "
                f"({(mb['reply_chars']/ma['reply_chars']):.1f}x)"
                if ma["reply_chars"]
                else ""
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
