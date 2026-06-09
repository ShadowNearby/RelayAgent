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
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (REPO_ROOT, SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents import _adb  # noqa: E402
from agents import _recorder  # noqa: E402
from agents.card_catalog import build_catalog  # noqa: E402
from agents.capability_matrix_router import load_matrix  # noqa: E402
from agents.flow_planner import FlowPlanner, PlanValidationError  # noqa: E402
from agents.flow_runner import FlowRunner  # noqa: E402
from agents.runtime_config import ensure_llm_env  # noqa: E402

GENERATED_DIR = REPO_ROOT / "manifests" / "_generated"
ENV_FILE = REPO_ROOT / ".env"
RECORD_TAIL_SECONDS = 10.0


# --------------------------------------------------------------------------- #
# cache (exact normalized-request match). Semantic reuse is a TODO.
# --------------------------------------------------------------------------- #


def _normalize(req: str) -> str:
    return " ".join((req or "").split()).strip()


def _plan_filename(req: str) -> str:
    norm = _normalize(req)
    h = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]
    slug = re.sub(r"\W+", "_", norm, flags=re.U).strip("_")[:24] or "plan"
    return f"{slug}_{h}.yaml"


def _cache_lookup(req: str) -> Path | None:
    """Return a persisted plan whose source_request matches `req` exactly."""
    norm = _normalize(req)
    if not GENERATED_DIR.exists():
        return None
    for path in sorted(GENERATED_DIR.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if _normalize(str(doc.get("source_request", ""))) == norm:
            # TODO(semantic-cache): fall back to embedding / LLM similarity
            # here when exact match misses, instead of regenerating.
            return path
    return None


def _persist(plan: dict, req: str) -> Path:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    norm = _normalize(req)
    ordered = {
        "flow_id": plan.get("flow_id") or ("gen_" + hashlib.sha1(norm.encode()).hexdigest()[:8]),
        "source_request": norm,
        "description": plan.get("description", ""),
        "apps_required": plan.get("apps_required", []),
        "steps": plan["steps"],
    }
    path = GENERATED_DIR / _plan_filename(req)
    path.write_text(
        yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return path


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


def _plan_packages(plan: dict) -> list[str]:
    """Every distinct app package the plan's steps will open, in step order."""
    pkgs: list[str] = []
    for step in plan.get("steps", []):
        pkg = step.get("app")
        if pkg and pkg not in pkgs:
            pkgs.append(pkg)
    return pkgs


def _prekill_apps(plan: dict) -> None:
    """Force-stop every app the plan touches before execution starts.

    Each leg's first predict still cold-launches its own app (force-stop +
    relaunch), but that only clears the leg's own app at the moment it opens —
    apps used in later legs (and the one handed off from) keep running in the
    background with stale state. Killing them all up front gives the whole plan
    a clean slate. Best-effort: a kill failure must not block the run.
    Disable with RELAY_PREKILL_APPS=0.
    """
    if os.getenv("RELAY_PREKILL_APPS", "1") == "0":
        return
    pkgs = _plan_packages(plan)
    if not pkgs:
        return
    logger.info(f"pre-kill background for {len(pkgs)} app(s): {', '.join(pkgs)}")
    for pkg in pkgs:
        try:
            _adb.force_stop(pkg)
        except Exception as e:  # noqa: BLE001 — best-effort; never block the run
            logger.warning(f"pre-kill force-stop {pkg} failed: {e}")


def _confirm() -> bool:
    """Ask y/N. Default N; a non-interactive stdin (EOF) means do not execute."""
    try:
        ans = input("Execute this plan? [y/N] ").strip().lower()
    except EOFError:
        print("(non-interactive input; defaulting to no execution)")
        return False
    return ans in ("y", "yes")


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
    llm = OpenAI(base_url=env["LLM_BASE_URL"], api_key=env["LLM_API_KEY"])
    mw_fallback = not args.no_mw_fallback
    planner = FlowPlanner(
        catalog, llm, env["LLM_MODEL"], matrix=matrix, mw_fallback=mw_fallback
    )

    # 1) cache lookup, else 2) synthesize + validate + persist.
    plan: dict[str, Any]
    plan_path: Path | None = None
    from_cache = False
    if not args.no_cache:
        hit = _cache_lookup(args.nl)
        if hit:
            plan = yaml.safe_load(hit.read_text(encoding="utf-8")) or {}
            if plan.get("unsatisfiable"):
                print(f"\nNo available app can satisfy this request: {plan.get('reason')}")
                return 1
            try:
                plan = planner.resolve_app_routes(plan, args.nl)
                planner.validate_plan(plan, args.nl)
            except PlanValidationError as e:
                logger.error(str(e))
                print("\nCached plan failed validation after rerouting; aborting.")
                print("Errors:")
                for err in e.errors:
                    print(f"  - {err}")
                print("\nRaw plan:")
                print(json.dumps(e.plan, ensure_ascii=False, indent=2))
                return 1
            plan_path = _persist(plan, args.nl)
            from_cache = True
            logger.info(f"cache hit → {hit.name}")
    if plan_path is None:
        try:
            plan = planner.plan(args.nl)
        except PlanValidationError as e:
            logger.error(str(e))
            print("\nGenerated plan still failed validation after LLM repair rounds; aborting.")
            print("Errors:")
            for err in e.errors:
                print(f"  - {err}")
            print("\nRaw plan:")
            print(json.dumps(e.plan, ensure_ascii=False, indent=2))
            return 1
        if plan.get("unsatisfiable"):
            print(f"\nNo available app can satisfy this request: {plan.get('reason')}")
            return 1
        plan_path = _persist(plan, args.nl)
        logger.info(f"plan persisted → {plan_path}")

    source = f"cache reuse ({plan_path.name})" if from_cache else "newly generated"
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
    _prekill_apps(plan)

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
        runner = FlowRunner(flow_path=plan_path, extra_args=extra)
        runner.run()
        completed = True
        return 0
    finally:
        if rec is not None:
            if completed:
                logger.info(f"task complete; keeping recording for {RECORD_TAIL_SECONDS:g}s")
                time.sleep(RECORD_TAIL_SECONDS)
            final = rec.stop()
            if final:
                logger.info(f"recording saved → {final}")


if __name__ == "__main__":
    sys.exit(main())
