#!/usr/bin/env python3
"""Human override channel for PhaseB VLM verdicts.

The PhaseB A/B verdicts in ``traj_logs/phaseB/<bench>/results.jsonl`` are produced
by RelayAgent's VLM leg judge (``agents/leg_judge.py``). The judge is wrong on some
tasks, so this tool lets a human override individual ``(id, system)`` verdicts
WITHOUT losing the original machine call and WITHOUT re-running anything.

Source of truth for human decisions is a sidecar ``manual_overrides.json`` next to
``results.jsonl`` — re-runnable and auditable. ``apply`` folds it INTO
``results.jsonl`` (the original VLM verdict is preserved under ``verdict_vlm`` the
first time, so apply is idempotent and reversible) and regenerates
``summary.json`` / ``summary.md`` with the existing aggregators, so every
downstream consumer (plots, tables) that reads ``verdict.status`` picks it up.

Overrides file shape::

    {"<task_id>": {"<system>": {"status": "success|failure|vlm", "reason": "..."}}}

``status: "vlm"`` reverts that cell back to the machine verdict (kept under
``verdict_vlm``). ``system`` is ``mw`` or ``relay``.

Typical loop (eyeball the screenshots, then record your calls)::

    uv run python scripts/eval/manual_judge.py sheet --bench mobileworld
    # open the listed *_final.png, decide, then for each wrong one:
    uv run python scripts/eval/manual_judge.py set  --bench mobileworld \
        --id CheckConferenceDurationTask --system relay --status success \
        --reason "alarm visibly set to 14:30 — judge misread the list"
    uv run python scripts/eval/manual_judge.py apply --bench mobileworld   # writes results + summary
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from run_benchmark_test import (  # noqa: E402
    _aggregate,
    _aggregate_by_app,
    _write_json,
    _write_markdown,
)

SYSTEMS = ("mw", "relay")
VALID_STATUS = ("success", "failure", "vlm")


def _bench_dir(bench: str) -> Path:
    d = REPO / "traj_logs" / "phaseB" / bench
    if not (d / "results.jsonl").exists():
        raise SystemExit(f"no results.jsonl under {d} — nothing to judge")
    return d


def _load_rows(bench_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in (bench_dir / "results.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]


def _id_to_task_dir(bench_dir: Path) -> dict[str, Path]:
    m: dict[str, Path] = {}
    for tj in sorted(glob.glob(str(bench_dir / "*" / "task.json"))):
        try:
            tid = json.loads(Path(tj).read_text(encoding="utf-8"))["id"]
        except Exception:
            continue
        m[tid] = Path(tj).parent
    return m


def _shot(task_dir: Path | None, system: str) -> str:
    if task_dir is None:
        return "(task dir not found)"
    p = task_dir / f"{system}_final.png"
    return str(p) if p.exists() else f"(missing: {p})"


def _instruction(task_dir: Path | None) -> str:
    if task_dir is None:
        return ""
    try:
        t = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        return str(t.get("instruction") or t.get("goal") or "")
    except Exception:
        return ""


def _overrides_path(bench_dir: Path) -> Path:
    return bench_dir / "manual_overrides.json"


def _load_overrides(bench_dir: Path) -> dict[str, dict[str, dict[str, Any]]]:
    p = _overrides_path(bench_dir)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _save_overrides(bench_dir: Path, ov: dict[str, Any]) -> None:
    _overrides_path(bench_dir).write_text(
        json.dumps(ov, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ────────────────────────────────── subcommands ──────────────────────────────────
def cmd_sheet(args: argparse.Namespace) -> int:
    bench_dir = _bench_dir(args.bench)
    rows = _load_rows(bench_dir)
    id2dir = _id_to_task_dir(bench_dir)
    ov = _load_overrides(bench_dir)

    rows = sorted(rows, key=lambda r: (r["id"], r["system"]))
    shown = 0
    for r in rows:
        if args.system and r["system"] != args.system:
            continue
        vlm = r.get("verdict_vlm") or r.get("verdict") or {}
        eff = r.get("verdict") or {}
        if args.only and (eff.get("status") != args.only):
            continue
        shown += 1
        tdir = id2dir.get(r["id"])
        human = (ov.get(r["id"], {}) or {}).get(r["system"])
        tag = f"  <-- override: {human['status']}" if human else ""
        print(f"\n[{r['id']}] {r['system']}")
        print(f"   instruction : {_instruction(tdir)[:140]}")
        print(f"   vlm verdict : {vlm.get('status','?').upper()}  — {str(vlm.get('reason',''))[:120]}")
        if r.get("verdict_vlm"):
            print(f"   effective   : {eff.get('status','?').upper()} (by {eff.get('by','vlm')}){tag}")
        elif human:
            print(f"   pending     : {human['status'].upper()} (not applied yet){tag}")
        print(f"   screenshot  : {_shot(tdir, r['system'])}")
    print(f"\n{shown} cell(s). edit with: manual_judge.py set --bench {args.bench} "
          f"--id <id> --system <mw|relay> --status <success|failure|vlm>")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    bench_dir = _bench_dir(args.bench)
    if args.status not in VALID_STATUS:
        raise SystemExit(f"--status must be one of {VALID_STATUS}")
    if args.system not in SYSTEMS:
        raise SystemExit(f"--system must be one of {SYSTEMS}")
    ids = {r["id"] for r in _load_rows(bench_dir)}
    if args.id not in ids:
        raise SystemExit(f"id {args.id!r} not in results.jsonl for {args.bench}")

    ov = _load_overrides(bench_dir)
    cell = ov.setdefault(args.id, {})
    if args.status == "vlm":
        cell.pop(args.system, None)
        if not cell:
            ov.pop(args.id, None)
        print(f"cleared override for [{args.id}] {args.system} (reverts to VLM on apply)")
    else:
        entry = {"status": args.status, "by": "human"}
        if args.reason:
            entry["reason"] = args.reason
        cell[args.system] = entry
        print(f"recorded [{args.id}] {args.system} -> {args.status.upper()}")
    _save_overrides(bench_dir, ov)
    print(f"saved {_overrides_path(bench_dir)} (run `apply` to fold into results.jsonl)")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    bench_dir = _bench_dir(args.bench)
    rows = _load_rows(bench_dir)
    ov = _load_overrides(bench_dir)
    systems = sorted({r["system"] for r in rows})

    changed = 0
    for r in rows:
        human = (ov.get(r["id"], {}) or {}).get(r["system"])
        has_vlm_backup = "verdict_vlm" in r
        if human:
            if not has_vlm_backup:
                r["verdict_vlm"] = r.get("verdict")
            new_status = human["status"]
            r["verdict"] = {
                "status": new_status,
                "score": 1.0 if new_status == "success" else 0.0,
                "reason": human.get("reason", "human override"),
                "by": "human",
            }
            changed += 1
        elif has_vlm_backup:
            # override was cleared/reverted — restore the machine verdict
            r["verdict"] = r.pop("verdict_vlm")
            changed += 1

    if args.dry_run:
        print(f"[dry-run] {changed} row(s) would change; results.jsonl not written")
        return 0

    out = bench_dir / "results.jsonl"
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                   encoding="utf-8")
    agg = _aggregate(rows, systems)
    final = {"benchmark": args.bench, "out_root": str(bench_dir),
             "n_tasks": len({r["id"] for r in rows}), "systems": systems,
             "by_system": agg, "by_app": _aggregate_by_app(rows, systems)}
    _write_json(bench_dir / "summary.json", final)
    _write_markdown(bench_dir / "summary.md", agg, systems, args.bench,
                    len({r["id"] for r in rows}))
    print(f"applied {changed} override row(s) -> {out}")
    print(f"regenerated {bench_dir/'summary.json'} + summary.md")
    print("\n" + json.dumps(agg, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sheet", help="list cells + screenshot paths to eyeball")
    p.add_argument("--bench", required=True)
    p.add_argument("--system", choices=SYSTEMS, help="filter to one system")
    p.add_argument("--only", choices=("success", "failure"),
                   help="filter to current effective status")
    p.set_defaults(func=cmd_sheet)

    p = sub.add_parser("set", help="record one human override")
    p.add_argument("--bench", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--system", required=True, choices=SYSTEMS)
    p.add_argument("--status", required=True, choices=VALID_STATUS,
                   help="success/failure, or 'vlm' to revert to the machine call")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("apply", help="fold overrides into results.jsonl + regen summary")
    p.add_argument("--bench", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_apply)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
