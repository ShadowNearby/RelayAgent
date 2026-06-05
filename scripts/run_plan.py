#!/usr/bin/env python3
"""Auto-synthesize and run a cross-app plan from one natural-language sentence.

Unlike `run_nl.py` — which *routes* a sentence to an existing hand-written
flow or single app — this entry *synthesizes* a brand-new multi-app plan with
the LLM (see `agents/flow_planner.py`), then executes it through the existing
`FlowRunner`. The generated plan uses the same step/bind schema as the
hand-written flows under `manifests/_flows/`, so no new executor is needed.

Pipeline:
    1. build_catalog()  — every app + capability (reused from run_nl).
    2. cache lookup     — exact normalized `source_request` match in
                          manifests/_generated/ (skip with --no-cache).
    3. FlowPlanner.plan — LLM emits a plan; validated locally (unknown ids,
                          dangling {var}, handoff-not-terminal, …). Repair on
                          failure is a TODO; for now we hard-fail.
    4. persist          — write the plan yaml to manifests/_generated/.
    5. preview + confirm — print the legs; execute only on y (default N).
    6. FlowRunner.run() — one `mw test` per app leg, reusing the persistent
                          MobileWorld server.

Usage:
    scripts/run_plan.py "在上海找三家评价好的小众书店，挑一家打车过去"
    scripts/run_plan.py "..." --dry-run     # plan + preview, don't execute
    scripts/run_plan.py "..." --yes         # skip the confirm prompt
    scripts/run_plan.py "..." --no-cache    # ignore any cached plan
    scripts/run_plan.py "..." --record      # screen-record the run
    scripts/run_plan.py "..." -- --step_wait_time 0.3   # forward to mw test
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
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

from agents import _recorder  # noqa: E402
from agents.flow_planner import FlowPlanner, PlanValidationError  # noqa: E402
from agents.flow_runner import FlowRunner, _load_dotenv  # noqa: E402
from run_nl import build_catalog  # noqa: E402

GENERATED_DIR = REPO_ROOT / "manifests" / "_generated"
ENV_FILE = REPO_ROOT / ".env"


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
    print(f"计划来源: {source}")
    if plan_path:
        try:
            print(f"plan 文件: {plan_path.relative_to(REPO_ROOT)}")
        except ValueError:
            print(f"plan 文件: {plan_path}")
    if plan.get("description"):
        print(f"描述: {plan['description']}")
    apps = plan.get("apps_required") or []
    if apps:
        print("需要的 app:")
        for a in apps:
            print(f"  - {_app_name(catalog, a.get('app_id'))} ({a.get('use_capability')})")
    print("步骤:")
    for n, s in enumerate(plan["steps"], 1):
        if s.get("type") == "ask_user":
            sel = s.get("select_from")
            tail = f"从 {sel} 选 1 → " if sel else ""
            print(f"  {n}. [ask_user] {tail}bind {s.get('bind')}")
            if s.get("prompt_header"):
                print(f"        “{_truncate(s['prompt_header'])}”")
        else:
            print(f"  {n}. [agent] {_app_name(catalog, s.get('app'))}/{s.get('capability')}")
            print(f"        → {_truncate(s.get('prompt', ''))}")
            if s.get("extract"):
                print(f"        ↳ extract → bind {s.get('bind')}")
            elif s.get("bind"):
                print(f"        ↳ bind {s.get('bind')}")
    print()


def _confirm() -> bool:
    """Ask y/N. Default N; a non-interactive stdin (EOF) means do not execute."""
    try:
        ans = input("执行这个 plan? [y/N] ").strip().lower()
    except EOFError:
        print("(非交互输入，默认不执行)")
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
    args, extra = p.parse_known_args(argv)

    env = _load_dotenv(ENV_FILE)
    for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        v = os.environ.get(k) or env.get(k)
        if not v:
            sys.exit(f"Missing required config: {k} (set in .env or shell env)")
        env[k] = v

    catalog = build_catalog()
    logger.info(f"catalog: {len(catalog['apps'])} apps, {len(catalog['flows'])} flows")

    # 1) cache lookup, else 2) synthesize + validate + persist.
    plan: dict[str, Any]
    plan_path: Path | None = None
    from_cache = False
    if not args.no_cache:
        hit = _cache_lookup(args.nl)
        if hit:
            plan = yaml.safe_load(hit.read_text(encoding="utf-8")) or {}
            plan_path = hit
            from_cache = True
            logger.info(f"cache hit → {hit.name}")
    if plan_path is None:
        llm = OpenAI(base_url=env["LLM_BASE_URL"], api_key=env["LLM_API_KEY"])
        planner = FlowPlanner(catalog, llm, env["LLM_MODEL"])
        try:
            plan = planner.plan(args.nl)
        except PlanValidationError as e:
            logger.error(str(e))
            print("\n生成的 plan 没通过校验，已中止（repair 暂未实现，见 flow_planner._repair TODO）。")
            print("错误：")
            for err in e.errors:
                print(f"  - {err}")
            print("\n原始 plan：")
            print(json.dumps(e.plan, ensure_ascii=False, indent=2))
            return 1
        if plan.get("unsatisfiable"):
            print(f"\n无法用现有 app 满足这个请求：{plan.get('reason')}")
            return 1
        plan_path = _persist(plan, args.nl)
        logger.info(f"plan persisted → {plan_path}")

    source = f"缓存复用 ({plan_path.name})" if from_cache else "新生成"
    _print_preview(plan, catalog, source, plan_path)

    if args.dry_run:
        print("--dry-run：只规划+预览，不执行。")
        return 0
    if not args.yes and not _confirm():
        print("已取消。")
        return 0

    # Reuse one persistent MW server across every leg's `mw test` (inject
    # --aw_host unless the caller passed their own). See scripts/_mw_server.py.
    if not any(a.startswith("--aw_host") or a.startswith("--aw-host") for a in extra):
        from _mw_server import ensure_server
        aw_host = ensure_server({**env, **os.environ})
        if aw_host:
            extra = ["--aw_host", aw_host, *extra]

    # Recording spans multiple `mw test` subprocesses (one per leg); keep a
    # single continuous parent-owned recording (mirrors run_nl's flow branch).
    rec = None
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
        runner = FlowRunner(flow_path=plan_path, extra_mw_args=extra)
        runner.run()
        return 0
    finally:
        if rec is not None:
            final = rec.stop()
            if final:
                logger.info(f"recording saved → {final}")


if __name__ == "__main__":
    sys.exit(main())
