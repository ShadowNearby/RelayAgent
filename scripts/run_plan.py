#!/usr/bin/env python3
"""Auto-synthesize and run a flow plan from one natural-language sentence.

This entry synthesizes a brand-new single- or multi-app plan with the LLM (see
`agents/flow_planner.py`), then executes it through `FlowRunner`. The generated
plan uses the step/bind schema `FlowRunner` executes, so no new executor is
needed.

Pipeline:
    1. build_catalog()  — every app + capability (shared card_catalog helper).
    2. cache lookup     — exact normalized `source_request` match in
                          manifests/_generated/ (skip with --no-cache).
    3. FlowPlanner.plan — LLM emits a plan; validated locally (unknown ids,
                          dangling {var}, handoff-not-terminal, …). Repair on
                          failure is a TODO; for now we hard-fail.
    4. persist          — write the plan yaml to manifests/_generated/.
    5. preview + confirm — print the legs; execute only on y (default N).
    6. FlowRunner.run() — one native runner subprocess per app leg (direct adb).

Usage:
    scripts/run_plan.py "在上海找三家评价好的小众书店，挑一家打车过去"
    scripts/run_plan.py "..." --dry-run     # plan + preview, don't execute
    scripts/run_plan.py "..." --yes         # skip the confirm prompt
    scripts/run_plan.py "..." --no-cache    # ignore any cached plan
    scripts/run_plan.py "..." --record      # screen-record the run
    scripts/run_plan.py "..." -- --step_wait_time 0.3   # forward to native runner
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (REPO_ROOT, SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.runtime import _recorder  # noqa: E402
from agents.flow import nl_flow  # noqa: E402
from agents.routing.card_catalog import build_catalog  # noqa: E402
from agents.routing.capability_matrix_router import load_matrix  # noqa: E402
from agents.flow.flow_planner import FlowPlanner  # noqa: E402
from agents.flow.flow_runner import _RecordingLLM  # noqa: E402
from agents.llm.llm_client import make_llm_client  # noqa: E402
from agents.flow.nl_flow import normalize_request as _normalize  # noqa: E402
from agents.runtime.runtime_config import ensure_llm_env  # noqa: E402

ENV_FILE = REPO_ROOT / ".env"
RECORD_TAIL_SECONDS = 10.0

# Cache / persist / pre-kill / execution live in agents.flow.nl_flow (shared with
# the Android entrypoint); this script keeps only the CLI frontend.


# --------------------------------------------------------------------------- #
# preview + confirm
# --------------------------------------------------------------------------- #


def _app_name(catalog: dict, app_id: str | None) -> str:
    for a in catalog.get("apps", []):
        if a.get("app_id") == app_id:
            return a.get("app_name") or a.get("agent_name") or app_id or "?"
    return app_id or "?"


def _truncate(s: str, n: int = 90) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _print_preview(plan: dict, catalog: dict, source: str, plan_path: Path | None) -> None:
    print()
    print(f"Plan source: {source}")
    if plan_path:
        try:
            print(f"Plan file: {plan_path.relative_to(REPO_ROOT)}")
        except ValueError:
            print(f"Plan file: {plan_path}")
    if plan.get("description"):
        print(f"Description: {plan['description']}")
    apps = plan.get("apps_required") or []
    if apps:
        print("Required apps:")
        for a in apps:
            print(f"  - {_app_name(catalog, a.get('app_id'))} ({a.get('use_capability')})")
    print("Steps:")
    for n, s in enumerate(plan["steps"], 1):
        if s.get("type") == "ask_user":
            sel = s.get("select_from")
            tail = f"choose 1 from {sel} -> " if sel else ""
            print(f"  {n}. [ask_user] {tail}bind {s.get('bind')}")
            if s.get("prompt_header"):
                print(f"        \"{_truncate(s['prompt_header'])}\"")
        elif s.get("type") == "mobileworld":
            hint = f" (app hint {s['app']})" if s.get("app") else ""
            print(f"  {n}. [MobileWorld fallback]{hint} — {s.get('x_fallback_reason') or 'uncovered by RA'}")
            print(f"        -> {_truncate(s.get('prompt', ''))}")
            if s.get("bind"):
                print(f"        -> bind {s.get('bind')}")
        else:
            print(f"  {n}. [agent] {_app_name(catalog, s.get('app'))}/{s.get('capability')}")
            print(f"        -> {_truncate(s.get('prompt', ''))}")
            if s.get("extract"):
                print(f"        -> extract -> bind {s.get('bind')}")
            elif s.get("bind"):
                print(f"        -> bind {s.get('bind')}")
    print()


def _confirm() -> bool:
    """Ask y/N. Default N; a non-interactive stdin (EOF) means do not execute."""
    try:
        ans = input("Execute this plan? [y/N] ").strip().lower()
    except EOFError:
        print("(non-interactive input; defaulting to no execution)")
        return False
    return ans in ("y", "yes")


# --------------------------------------------------------------------------- #
# token / latency accounting
# --------------------------------------------------------------------------- #


def _call_metrics(call: dict, phase: str, leg: str | None) -> dict:
    """Normalize one recorded LLM call to a flat metrics row.

    Handles both shapes we log: the planner / flow recorder uses `usage`
    ({prompt,completion,total}_tokens); the in-app agent uses `usage_delta`
    ({prompt,completion,cached}_tokens, no total). Missing totals are derived.
    """
    usage = call.get("usage") or {}
    delta = call.get("usage_delta") or {}
    prompt = usage.get("prompt_tokens")
    if prompt is None:
        prompt = delta.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if completion is None:
        completion = delta.get("completion_tokens")
    total = usage.get("total_tokens")
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)
    return {
        "phase": phase,
        "leg": leg,
        "purpose": call.get("purpose"),
        "model": call.get("model"),
        "ts": call.get("ts"),
        "latency_s": call.get("elapsed_s"),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": delta.get("cached_tokens"),
        "error": call.get("error"),
    }


def _agg(rows: list[dict]) -> dict:
    return {
        "calls": len(rows),
        "prompt_tokens": sum(r["prompt_tokens"] or 0 for r in rows),
        "completion_tokens": sum(r["completion_tokens"] or 0 for r in rows),
        "total_tokens": sum(r["total_tokens"] or 0 for r in rows),
        "latency_s": round(sum(r["latency_s"] or 0 for r in rows), 2),
    }


def _sum_aggs(aggs: list[dict]) -> dict:
    return {
        "calls": sum(a["calls"] for a in aggs),
        "prompt_tokens": sum(a["prompt_tokens"] for a in aggs),
        "completion_tokens": sum(a["completion_tokens"] for a in aggs),
        "total_tokens": sum(a["total_tokens"] for a in aggs),
        "latency_s": round(sum(a["latency_s"] for a in aggs), 2),
    }


def _read_summary_usage(leg_dir: Path) -> dict | None:
    """The leg's authoritative agent token total, written by native_runner from
    `agent.get_total_token_usage()`. Preferred over summing the per-call
    `0.llm_calls` log, which can drop calls written before traj.json exists."""
    p = leg_dir / "summary.json"
    if not p.exists():
        return None
    try:
        u = (json.loads(p.read_text(encoding="utf-8")) or {}).get("token_usage") or {}
    except (json.JSONDecodeError, OSError):
        return None
    return u or None


def _gather(
    plan_calls: list[dict], flow_traj_root: Path | None
) -> tuple[list[tuple[str, dict]], list[dict], dict[str, dict]]:
    """Collect usage across the whole run_plan.

    Returns (table, detail, by_phase):
      - table: ordered (label, agg) rows to print — plan, then per leg the
        flow-process and in-app agent buckets.
      - detail: every individual call record (for the JSON dump).
      - by_phase: {plan|flow|agent: agg}.
    """
    table: list[tuple[str, dict]] = []
    detail: list[dict] = []

    plan_rows = [_call_metrics(c, "plan", None) for c in plan_calls]
    detail += plan_rows
    if plan_rows:
        table.append(("plan", _agg(plan_rows)))

    flow_rows_all: list[dict] = []
    agent_aggs: list[dict] = []
    if flow_traj_root and flow_traj_root.exists():
        for leg_dir in sorted(p for p in flow_traj_root.iterdir() if p.is_dir()):
            traj = leg_dir / "traj.json"
            if not traj.exists():
                continue
            try:
                data = json.loads(traj.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            leg = leg_dir.name
            flow_rows = [_call_metrics(c, "flow", leg)
                         for c in data.get("flow_llm_calls") or []]
            agent_rows = [_call_metrics(c, "agent", leg)
                          for c in (data.get("0") or {}).get("llm_calls") or []]
            detail += flow_rows + agent_rows
            flow_rows_all += flow_rows
            if flow_rows:
                table.append((f"{leg} · flow", _agg(flow_rows)))

            # Agent tokens: trust summary.json's authoritative per-leg total;
            # fall back to the per-call sum only when summary is absent. Latency
            # always comes from the per-call log (summary carries no timing).
            ag = _agg(agent_rows)
            su = _read_summary_usage(leg_dir)
            if su and (ag["total_tokens"] == 0 or not agent_rows):
                prompt = su.get("prompt_tokens") or 0
                completion = su.get("completion_tokens") or 0
                ag = {
                    "calls": len(agent_rows),
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": su.get("total_tokens") or (prompt + completion),
                    "latency_s": ag["latency_s"],
                }
            if ag["calls"] or ag["total_tokens"]:
                table.append((f"{leg} · agent", ag))
                agent_aggs.append(ag)

    by_phase = {
        "plan": _agg(plan_rows),
        "flow": _agg(flow_rows_all),
        "agent": _sum_aggs(agent_aggs) if agent_aggs else _agg([]),
    }
    return table, detail, by_phase


def _report_usage(
    plan_calls: list[dict], flow_traj_root: Path | None, request: str
) -> None:
    """Print a token/latency breakdown for the whole run and, when a flow traj
    root exists, persist the per-call log to token_usage.json there.
    Best-effort: never let accounting break the run."""
    try:
        table, detail, by_phase = _gather(plan_calls, flow_traj_root)
        if not table:
            return
        total = _sum_aggs(list(by_phase.values()))

        print("\nLLM usage (calls / prompt / completion / total tokens / latency):")
        hdr = (f"  {'group':<28}{'calls':>6}{'prompt':>11}"
               f"{'compl.':>10}{'total':>11}{'lat(s)':>9}")
        rule = "  " + "-" * (len(hdr) - 2)
        print(hdr)
        print(rule)
        for label, a in table:
            print(
                f"  {label[:28]:<28}{a['calls']:>6}{a['prompt_tokens']:>11}"
                f"{a['completion_tokens']:>10}{a['total_tokens']:>11}{a['latency_s']:>9.1f}"
            )
        print(rule)
        print(
            f"  {'TOTAL':<28}{total['calls']:>6}{total['prompt_tokens']:>11}"
            f"{total['completion_tokens']:>10}{total['total_tokens']:>11}{total['latency_s']:>9.1f}"
        )
        print()

        if flow_traj_root and flow_traj_root.exists():
            out = flow_traj_root / "token_usage.json"
            payload = {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "request": _normalize(request),
                "total": total,
                "by_phase": by_phase,
                "by_group": {label: a for label, a in table},
                "calls": detail,
            }
            out.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            try:
                print(f"Token usage written → {out.relative_to(REPO_ROOT)}")
            except ValueError:
                print(f"Token usage written → {out}")
    except Exception as e:  # noqa: BLE001 — accounting must never break the run
        logger.warning(f"token usage report failed: {e}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("nl", help="The natural-language request")
    p.add_argument("--dry-run", action="store_true",
                   help="Synthesize + preview the plan but don't execute")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip the confirm prompt and execute")
    p.add_argument("--no-cache", action="store_true",
                   help="Ignore any cached plan; always regenerate")
    p.add_argument("--record", nargs="?", const="", default=None, metavar="DIR",
                   help="Record device screen via adb screenrecord. "
                        "Optional DIR overrides traj_logs/recordings/<ts>/.")
    p.add_argument("--no-mw-fallback", action="store_true",
                   help="Disable the MobileWorld fallback: a leg RA can't cover "
                        "makes the plan unsatisfiable instead of running via "
                        "MobileWorld's general_e2e agent. Same as RELAY_MW_FALLBACK=0.")
    args, extra = p.parse_known_args(argv)

    try:
        env = ensure_llm_env(ENV_FILE)
    except RuntimeError as e:
        sys.exit(str(e))

    catalog = build_catalog()
    matrix = load_matrix()
    logger.info(
        f"catalog: {len(catalog['apps'])} apps; "
        f"matrix: {len(matrix['cap_desc'])} capabilities, {len(matrix['app_ids'])} apps"
    )
    # Wrap the planner's client so every plan-phase LLM call (synthesis,
    # repair, slot extraction, locale rewrite, and the three-stage routing
    # inside capability_matrix_router) is recorded with latency + token usage.
    # retry=False because the planner / router already wrap calls in
    # create_with_retry — see _RecordingLLM.
    llm = _RecordingLLM(
        make_llm_client(env["LLM_BASE_URL"], env["LLM_API_KEY"]),
        retry=False,
    )
    llm.purpose = "plan"
    mw_fallback = not args.no_mw_fallback
    planner = FlowPlanner(
        catalog, llm, env["LLM_MODEL"], matrix=matrix, mw_fallback=mw_fallback
    )

    # The flow traj root is only known once execution starts; capture it so
    # the finally below can harvest each leg's token log into the report.
    flow_traj_root: Path | None = None
    try:
        # 1) cache lookup, else 2) synthesize + validate + persist — shared
        # pipeline in agents.flow.nl_flow; this frontend only renders the outcome.
        result = nl_flow.plan_request(
            args.nl, planner=planner, use_cache=not args.no_cache
        )
        if result.unsatisfiable:
            print(f"\nNo available app can satisfy this request: {result.reason}")
            return 1
        if result.validation is not None:
            e = result.validation
            if result.from_cache:
                print("\nCached plan failed validation after rerouting; aborting.")
            else:
                print("\nGenerated plan still failed validation after LLM repair rounds; aborting.")
            print("Errors:")
            for err in e.errors:
                print(f"  - {err}")
            print("\nRaw plan:")
            print(json.dumps(e.plan, ensure_ascii=False, indent=2))
            return 1
        plan, plan_path = result.plan, result.plan_path

        source = f"cache reuse ({plan_path.name})" if result.from_cache else "newly generated"
        _print_preview(plan, catalog, source, plan_path)

        if args.dry_run:
            print("--dry-run: plan and preview only; not executing.")
            return 0
        if not args.yes and not _confirm():
            print("Canceled.")
            return 0

        # No server: each leg is a direct-adb native runner subprocess (FlowRunner
        # forwards `extra` to the runner verbatim).

        # Clean slate: kill the background of every app the plan touches before the
        # first leg launches (per-leg cold-launch only clears that leg's own app).
        # Done here (not in execute_plan) so it lands BEFORE recording starts.
        nl_flow.prekill_apps(plan)

        # Recording spans multiple leg subprocesses (one per leg); keep a
        # single continuous parent-owned recording across all app legs.
        rec = None
        completed = False
        if args.record is not None:
            out_dir = (
                Path(args.record).expanduser().resolve()
                if args.record
                else REPO_ROOT / "traj_logs" / "recordings" / datetime.now().strftime("%Y%m%d_%H%M%S")
            )
            os.environ.setdefault("RELAY_SKIP_STEP_SCREENSHOT", "1")
            logger.info("recording mode → RELAY_SKIP_STEP_SCREENSHOT=1")
            rec = _recorder.start(out_dir)
            logger.info(f"screen recording (parent-owned) → {out_dir}")

        try:
            outcome = nl_flow.execute_plan(plan_path, extra_args=extra, prekill=False)
            flow_traj_root = outcome.flow_traj_root
            completed = True
            return 0
        except nl_flow.FlowExecutionError as err:
            # Keep the traj root for the usage report, then surface the
            # original leg failure exactly as before the extraction.
            flow_traj_root = err.flow_traj_root
            raise err.original
        finally:
            if rec is not None:
                if completed:
                    logger.info(f"task complete; keeping recording for {RECORD_TAIL_SECONDS:g}s")
                    time.sleep(RECORD_TAIL_SECONDS)
                final = rec.stop()
                if final:
                    logger.info(f"recording saved → {final}")
    finally:
        # Always report what the run cost — plan-phase calls (from the recorder)
        # plus any per-leg agent/flow calls harvested from the flow traj root.
        _report_usage(llm.calls, flow_traj_root, args.nl)


if __name__ == "__main__":
    sys.exit(main())
