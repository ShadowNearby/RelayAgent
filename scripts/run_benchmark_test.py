#!/usr/bin/env python3
"""One-click A/B benchmark driver: run the SAME tasks through multiple agent systems.

Default benchmark is the **MobileWorld** task suite, pulled from the HuggingFace
dataset ``Tongyi-MAI/MobileWorld`` (``task_info.csv``, 201 tasks). Each task is a
natural-language goal; this driver runs it through both systems on one real device:

  relay : RelayAgent's NL cross-app flow via ``scripts/run_plan.py`` — it
          auto-synthesizes a single/multi-app plan from the goal and executes it
          (one direct-adb native-runner subprocess per app leg).
  mw    : MobileWorld's own GUI agent via ``scripts/run_mobileworld.py``
          (``mw test``, agent-type ``general_e2e`` by default). One shared
          MobileWorld server is started once and reused for every task.

Per (task, system) it records:
  - completion (success / failure), judged UNIFORMLY by RelayAgent's VLM leg judge
    (``agents/leg_judge.py``) reading the goal + a fresh post-run screenshot — so
    both systems are held to the same yardstick;
  - wall-clock duration of the run (whole-task subprocess time);
  - LLM token usage (prompt / completion / total).

Time/token aggregates are reported over the COMPLETED tasks only.

EXTENSIBILITY — two registries let this grow without surgery:
  * BENCHMARKS  — a benchmark is a task source: how to load + how to pick a smoke
                  subset (``--benchmark``). Tasks are normalized to the contract in
                  ``_normalize_task`` so every system + the judge consume them the
                  same way. ``mobileworld`` (HF) and ``single_app`` (local yaml) ship.
  * SYSTEMS     — a system is one agent runner ``(task, sys_dir, ctx) -> metrics``
                  (``--systems``). Add an entry to A/B a third agent.

NOTE on coverage: the MobileWorld apps (Mail/Messages/Mastodon/Calendar/Files/…)
mostly have no RelayAgent manifest and may not be installed on a real device; only
Maps/MCP-Amap overlaps (高德). Expect many tasks to fail for BOTH systems on a real
device — that is an honest, same-device, same-judge comparison. ``--filter-supported``
trims to the tasks whose apps RelayAgent can actually attempt.

CAVEAT on relay tokens: we sum every leg's in-app tokens + its flow-process tokens
(leg judge / bind extraction). The one-shot plan-synthesis call in run_plan.py is
NOT yet captured, so relay token totals slightly under-count planning overhead.

Real device + USB debugging required (see CLAUDE.md). ``.env`` must hold
``LLM_BASE_URL`` / ``LLM_API_KEY`` / ``LLM_MODEL`` — the judge and both systems read it.

Outputs land under ``traj_logs/benchmark_<name>_<ts>/``:
  <NN>_<task-id>/ {task.json, <sys>/, <sys>_final.png, <sys>_result.json}
  results.jsonl              one line per (task, system), appended live
  summary.json / summary.md  aggregate completion-rate + time + token tables

Examples:
    uv run python scripts/run_benchmark_test.py --dry-list           # MobileWorld smoke set
    uv run python scripts/run_benchmark_test.py --all                # full 201
    uv run python scripts/run_benchmark_test.py --filter-supported   # RA-attemptable subset
    uv run python scripts/run_benchmark_test.py --systems relay --no-judge
    uv run python scripts/run_benchmark_test.py --benchmark single_app
    uv run python scripts/run_benchmark_test.py --only-id AddBusinessTripAskUserTask
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from openai import OpenAI  # noqa: E402

from agents import leg_judge  # noqa: E402
from agents._adb import screencap as adb_screencap  # noqa: E402
from agents.runtime_config import ensure_llm_env  # noqa: E402

# Server lifecycle + MobileWorld runtime resolution are reused from the existing
# single-goal driver so the two stay in lockstep.
import run_mobileworld as mw_driver  # noqa: E402

ENV_FILE = REPO_ROOT / ".env"
TRAJ_LOGS = REPO_ROOT / "traj_logs"
RUN_PLAN = SCRIPTS_DIR / "run_plan.py"
RUN_MOBILEWORLD = SCRIPTS_DIR / "run_mobileworld.py"

MW_DATASET = "Tongyi-MAI/MobileWorld"

# MobileWorld app name -> RelayAgent manifest app id. Used by --filter-supported
# and for display; extend this as RelayAgent gains manifests for more apps.
MW_APP_TO_RA = {
    "Maps": "com.autonavi.minimap",
    "MCP-Amap": "com.autonavi.minimap",
}


# ───────────────────────────── small io helpers ─────────────────────────────
def _slug(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", s).strip("_") or "task"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _norm_tokens(tok: Any) -> dict[str, int | None]:
    if not isinstance(tok, dict):
        tok = {}
    return {
        "prompt_tokens": tok.get("prompt_tokens"),
        "completion_tokens": tok.get("completion_tokens"),
        "total_tokens": tok.get("total_tokens"),
    }


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ BENCHMARKS — a task source. To add a suite: write a loader + smoke picker, ║
# ║ register it below. Tasks are normalized to the contract in `_normalize_task`║
# ║ so every system + the judge consume them uniformly.                        ║
# ╚══════════════════════════════════════════════════════════════════════════╝
@dataclass
class Benchmark:
    name: str
    # load(path_or_None) -> (suite_meta, normalized_tasks)
    load: Callable[[Path | None], tuple[dict[str, Any], list[dict[str, Any]]]]
    # smoke(tasks, per_app) -> 1-based indices of a small representative subset
    smoke: Callable[[list[dict[str, Any]], int], list[int]]
    # mw `mw test` prelaunches the target app only when its apps are real device
    # packages (single_app). MobileWorld tasks are multi-app / MCP, so skip it.
    mw_prelaunch: bool = True


def _normalize_task(raw: dict[str, Any]) -> dict[str, Any]:
    """The minimal contract every benchmark task must expose to this driver.

    Required: id, instruction. Optional (used when present):
      app               — primary app id/name (display + smoke grouping)
      apps              — list of apps the task touches
      capability        — display only (relay routes via run_plan)
      success           — explicit done-rubric; fed to the judge as the bar
      category          — smoke ordering / display
      handoff_required  — when true, mw is told to stop before the irreversible CTA
      stop_before       — per-task override of the mw stop-hint phrasing
      lang / tags       — provenance
    """
    if not raw.get("id") or not raw.get("instruction"):
        raise SystemExit(f"task missing id/instruction: {raw!r}")
    apps = raw.get("apps") or ([raw["app"]] if raw.get("app") else [])
    return {
        "id": raw["id"],
        "instruction": raw["instruction"],
        "app": raw.get("app") or (apps[0] if apps else ""),
        "apps": apps,
        "capability": raw.get("capability", ""),
        "success": raw.get("success"),
        "category": raw.get("category"),
        "difficulty": raw.get("difficulty"),
        "handoff_required": bool(raw.get("handoff_required")),
        "stop_before": raw.get("stop_before") or raw.get("mw_stop_hint"),
        "lang": raw.get("lang"),
        "tags": raw.get("tags"),
    }


# ---- benchmark: mobileworld (HuggingFace Tongyi-MAI/MobileWorld) ----
def _load_mobileworld(path: Path | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if path is None:
        try:
            from huggingface_hub import hf_hub_download
        except ImportError as e:
            raise SystemExit("huggingface_hub is required to load the MobileWorld dataset") from e
        path = Path(hf_hub_download(MW_DATASET, "task_info.csv", repo_type="dataset"))
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    if not rows:
        raise SystemExit(f"No tasks in {path}")
    tasks: list[dict[str, Any]] = []
    for r in rows:
        apps = [a.strip() for a in (r.get("Apps") or "").split(",") if a.strip()]
        tags = [t.strip() for t in (r.get("Tags") or "").split(",") if t.strip()]
        lang = "cn" if "lang-cn" in tags else ("en" if "lang-en" in tags else None)
        tasks.append(_normalize_task({
            "id": r["Task Name"],
            "instruction": r["Goal"],
            "apps": apps,
            "category": r.get("Task Type"),
            "handoff_required": "agent-user-interaction" in tags,
            "lang": lang,
            "tags": tags,
        }))
    meta = {"suite": "MobileWorld", "source": MW_DATASET, "n_tasks": len(tasks)}
    return meta, tasks


def _smoke_mobileworld(tasks: list[dict[str, Any]], per_app: int) -> list[int]:
    """Per first-app, prefer single-app lang-cn tasks (link-validation friendly)."""
    def rank(t: dict[str, Any]) -> tuple[int, int]:
        single = 0 if t.get("category") == "Single-app" else 1
        cn = 0 if t.get("lang") == "cn" else 1
        return (single, cn)

    return _smoke_by_app(tasks, per_app, rank)


# ---- benchmark: single_app (local RelayAgent yaml) ----
def _load_single_app(path: Path | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = path or (REPO_ROOT / "benchmark" / "single_app_tasks.yaml")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = doc.get("tasks") or []
    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"No tasks found in {path}")
    meta = {"suite": doc.get("suite"), "version": doc.get("version")}
    return meta, [_normalize_task(t) for t in raw]


def _smoke_single_app(tasks: list[dict[str, Any]], per_app: int) -> list[int]:
    """Up to `per_app` tasks per app, preferring read-only easy ones."""
    def rank(t: dict[str, Any]) -> tuple[int, int]:
        ro = 0 if (t.get("category") == "info_qa" and not t.get("handoff_required")) else 1
        easy = {"easy": 0, "medium": 1, "hard": 2}.get(t.get("difficulty"), 3)
        return (ro, easy)

    return _smoke_by_app(tasks, per_app, rank)


def _smoke_by_app(tasks: list[dict[str, Any]], per_app: int,
                  rank: Callable[[dict[str, Any]], tuple]) -> list[int]:
    by_app: dict[str, list[int]] = {}
    for i, t in enumerate(tasks, 1):
        by_app.setdefault(t["app"], []).append(i)
    chosen: list[int] = []
    for idxs in by_app.values():
        chosen.extend(sorted(idxs, key=lambda i: rank(tasks[i - 1]))[:per_app])
    return sorted(chosen)


BENCHMARKS: dict[str, Benchmark] = {
    "mobileworld": Benchmark("mobileworld", _load_mobileworld, _smoke_mobileworld, mw_prelaunch=False),
    "single_app": Benchmark("single_app", _load_single_app, _smoke_single_app, mw_prelaunch=True),
}
DEFAULT_BENCHMARK = "mobileworld"


def _supported(task: dict[str, Any]) -> bool:
    """True if RelayAgent has a manifest for every app the task touches."""
    apps = task.get("apps") or []
    return bool(apps) and all(a in MW_APP_TO_RA for a in apps)


def _select(
    tasks: list[dict[str, Any]],
    smoke: Callable[[list[dict[str, Any]], int], list[int]],
    *,
    only_ids: set[str] | None,
    run_all: bool,
    per_app: int,
    limit: int | None,
    filter_supported: bool,
) -> list[tuple[int, dict[str, Any]]]:
    if only_ids:
        indexed = [(i, t) for i, t in enumerate(tasks, 1) if t.get("id") in only_ids]
    elif run_all:
        indexed = list(enumerate(tasks, 1))
    else:
        keep = set(smoke(tasks, per_app))
        indexed = [(i, t) for i, t in enumerate(tasks, 1) if i in keep]
    if filter_supported:
        indexed = [(i, t) for i, t in indexed if _supported(t)]
    if limit is not None:
        indexed = indexed[:limit]
    return indexed


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ SYSTEMS — one agent runner each. Signature: (task, sys_dir, ctx) -> metrics║
# ║ {returncode, timed_out, elapsed_s, tokens, steps, terminal_action, reply}. ║
# ╚══════════════════════════════════════════════════════════════════════════╝
@dataclass
class RunCtx:
    env: dict[str, str]
    timeout_s: float | None
    mw_prelaunch: bool
    step_wait: str = "0.5"            # relay
    server_url: str = ""             # mw
    mw_agent_type: str = "general_e2e"
    mw_max_round: int = 25


# mw has no built-in handoff convention; for handoff tasks we ASK it to stop before
# the irreversible CTA so it is compared on the same "stop at the boundary" bar as
# relay (which honors handoff_required natively). Phrasing is per-category, with a
# per-task `stop_before` override.
_STOP_HINTS = {
    "transaction": "做到提交订单/支付这一步之前就停下，把最终确认交给用户——不要真的下单或付款。",
    "navigation": "把路线和预计时间准备好后停下，不要点“开始导航”。",
    "content_gen": "把内容生成好后停在“打开/导出文档”之前，不要真的导出或打开最终文件。",
}


def _handoff_stop_hint(task: dict[str, Any]) -> str:
    if not task.get("handoff_required"):
        return ""
    if task.get("stop_before"):
        return f"重要：{task['stop_before']}"
    generic = "在执行任何不可逆的提交/支付/确认操作之前停下，把最终确认交给用户。"
    return "重要：" + _STOP_HINTS.get(task.get("category"), generic)


def _run_subprocess(cmd: list[str], sys_dir: Path, *, env: dict[str, str] | None,
                    timeout_s: float | None) -> tuple[int, bool, float]:
    sys_dir.mkdir(parents=True, exist_ok=True)
    _write_json(sys_dir / "command.json", {"cmd": cmd})
    t0 = time.monotonic()
    timed_out = False
    with (sys_dir / "stdout.log").open("wb") as out, (sys_dir / "stderr.log").open("wb") as err:
        proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=err)
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            rc = proc.wait()
    return rc, timed_out, round(time.monotonic() - t0, 1)


def _run_mw(task: dict[str, Any], sys_dir: Path, ctx: RunCtx) -> dict[str, Any]:
    """System `mw`: run the task through MobileWorld via run_mobileworld.py."""
    goal = task["instruction"]
    hint = _handoff_stop_hint(task)
    if hint:
        goal = f"{goal}\n{hint}"
    cmd = [
        sys.executable, str(RUN_MOBILEWORLD), goal,
        "--no-start-server",                # reuse the server this driver started
        "--server-url", ctx.server_url,
        "--agent-type", ctx.mw_agent_type,
        "--max-round", str(ctx.mw_max_round),
        "--timeout", str(int(ctx.timeout_s) if ctx.timeout_s else 600),
        "--output", str(sys_dir),           # forwarded to `mw test` → <sys_dir>/user_task/
    ]
    if ctx.mw_prelaunch and task.get("app"):
        cmd += ["--app", task["app"]]
    else:
        cmd += ["--no-prelaunch"]
    rc, timed_out, elapsed = _run_subprocess(cmd, sys_dir, env=None, timeout_s=ctx.timeout_s)

    # MobileWorld's TrajLogger writes <sys_dir>/user_task/traj.json with token_usage.
    traj = _read_json(sys_dir / "user_task" / "traj.json") or {}
    node = traj.get("0") if isinstance(traj.get("0"), dict) else {}
    tokens = node.get("token_usage") or traj.get("token_usage") or {}
    steps_list = node.get("traj") if isinstance(node.get("traj"), list) else []
    terminal_action = None
    if steps_list:
        terminal_action = (steps_list[-1].get("action") or {}).get("action_type")
    return {
        "returncode": rc, "timed_out": timed_out, "elapsed_s": elapsed,
        "tokens": _norm_tokens(tokens), "steps": len(steps_list) or None,
        "terminal_action": terminal_action,
        "reply": "",  # mw test persists no textual reply; the judge reads the screen
        "goal_sent": goal,
    }


_FLOW_ROOT_RE = re.compile(r"flow traj root:\s*(\S+)")


def _find_flow_root(stderr_log: Path, before: set[Path]) -> Path | None:
    """Locate the FlowRunner output dir for a run_plan invocation.

    Primary: parse the "flow traj root: <path>" line run_plan logs. Fallback:
    the newest traj_logs/* dir that did not exist before this run.
    """
    try:
        for line in stderr_log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _FLOW_ROOT_RE.search(line)
            if m and Path(m.group(1)).exists():
                return Path(m.group(1))
    except OSError:
        pass
    new = [p for p in TRAJ_LOGS.glob("*") if p.is_dir() and p not in before]
    return max(new, key=lambda p: p.stat().st_mtime) if new else None


def _harvest_relay_legs(flow_root: Path) -> dict[str, Any]:
    """Sum in-app + flow-process tokens across a flow's legs; collect leg verdicts."""
    prompt = completion = total = steps = 0
    legs: list[dict[str, Any]] = []
    last_reply = ""
    last_terminal = None
    for leg in sorted(p for p in flow_root.glob("[0-9]*_*") if p.is_dir()):
        summary = _read_json(leg / "summary.json") or {}
        tok = _norm_tokens(summary.get("token_usage") or {})
        prompt += tok["prompt_tokens"] or 0
        completion += tok["completion_tokens"] or 0
        total += tok["total_tokens"] or 0
        steps += summary.get("steps") or 0
        last_terminal = summary.get("last_action_type") or last_terminal
        for call in ((_read_json(leg / "traj.json") or {}).get("flow_llm_calls") or []):
            u = call.get("usage") or {}
            prompt += u.get("prompt_tokens") or 0
            completion += u.get("completion_tokens") or 0
            total += u.get("total_tokens") or 0
        verdict = _read_json(leg / "leg_verdict.json")
        if isinstance(verdict, dict):
            legs.append({"step": verdict.get("step"), "status": verdict.get("status")})
        reply_doc = _read_json(leg / "agent_reply.json")
        if isinstance(reply_doc, dict):
            last_reply = reply_doc.get("reply") or reply_doc.get("text") or last_reply
    return {
        "tokens": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total},
        "steps": steps or None, "legs": legs, "reply": last_reply, "terminal_action": last_terminal,
    }


def _run_relay(task: dict[str, Any], sys_dir: Path, ctx: RunCtx) -> dict[str, Any]:
    """System `relay`: run the task through RelayAgent's NL flow (run_plan.py)."""
    cmd = [
        sys.executable, str(RUN_PLAN), "--yes", "--no-cache", task["instruction"],
        "--", "--step_wait_time", ctx.step_wait,
    ]
    child_env = {**ctx.env, **os.environ, "RELAY_WAIT_SECONDS": os.getenv("RELAY_WAIT_SECONDS", "0.2")}
    before = {p for p in TRAJ_LOGS.glob("*") if p.is_dir()} if TRAJ_LOGS.is_dir() else set()
    rc, timed_out, elapsed = _run_subprocess(cmd, sys_dir, env=child_env, timeout_s=ctx.timeout_s)

    flow_root = _find_flow_root(sys_dir / "stderr.log", before)
    harvested: dict[str, Any] = {"tokens": _norm_tokens({}), "steps": None, "legs": [],
                                 "reply": "", "terminal_action": None}
    flow_root_str = None
    if flow_root is not None:
        harvested = _harvest_relay_legs(flow_root)
        flow_root_str = str(flow_root)
    return {
        "returncode": rc, "timed_out": timed_out, "elapsed_s": elapsed,
        "tokens": harvested["tokens"], "steps": harvested["steps"],
        "terminal_action": harvested["terminal_action"], "reply": harvested["reply"],
        "relay_legs": harvested["legs"], "flow_root": flow_root_str,
    }


SYSTEMS: dict[str, Callable[[dict[str, Any], Path, RunCtx], dict[str, Any]]] = {
    "relay": _run_relay,
    "mw": _run_mw,
}


# ───────────────────────────── uniform VLM judge ─────────────────────────────
def _judge(llm: OpenAI, model: str, task: dict[str, Any], metrics: dict[str, Any],
           final_png: Path) -> dict[str, Any]:
    """Score one finished run with leg_judge against a fresh post-run screenshot.

    The goal handed to the judge is the user instruction (PLUS the task's explicit
    `success` rubric when the benchmark provides one), so both systems are held to
    the same definition of done. A run the judge calls `loading` is re-shot once
    and re-judged (settle retry).
    """
    goal = task["instruction"]
    if task.get("success"):
        goal = f"{goal}\n\n[完成判定标准] {task['success']}"

    shot = adb_screencap()
    if shot is not None:
        try:
            shot.save(final_png)
        except OSError:
            pass

    common = dict(
        llm=llm, model=model, goal=goal, app=task.get("app", ""),
        capability=task.get("capability", ""), reply=metrics.get("reply", ""),
        frames=[], terminal_action=metrics.get("terminal_action"),
    )
    verdict = leg_judge.judge_leg(**common, live_image=shot)
    if verdict.status == leg_judge.LOADING:
        time.sleep(3.0)
        shot2 = adb_screencap()
        if shot2 is not None:
            try:
                shot2.save(final_png)
            except OSError:
                pass
            verdict = leg_judge.judge_leg(**common, live_image=shot2)
    return verdict.to_dict()


# ───────────────────────── RA routing / planning only ─────────────────────────
def _plan_legs(plan: dict[str, Any]) -> list[dict[str, str]]:
    """App legs (app + resolved capability) of a synthesized plan, in order."""
    legs: list[dict[str, str]] = []
    for step in plan.get("steps") or []:
        if isinstance(step, dict) and (step.get("app") or step.get("capability")):
            legs.append({"app": step.get("app", ""), "capability": step.get("capability", "")})
    return legs


def _run_plan_only(selected: list[tuple[int, dict[str, Any]]], env: dict[str, str],
                   out_root: Path) -> int:
    """For each task, run ONLY RelayAgent's plan synthesis + route resolution.

    Pure LLM, no device / server / judge. Shows how every benchmark goal would be
    decomposed into a RelayAgent flow (which app+capability per leg), or why it is
    unsatisfiable / invalid. Mirrors run_plan.py's planning stage (--no-cache).
    """
    from agents.card_catalog import build_catalog
    from agents.capability_matrix_router import load_matrix
    from agents.flow_planner import FlowPlanner, PlanValidationError

    catalog = build_catalog()
    matrix = load_matrix()
    llm = OpenAI(base_url=env["LLM_BASE_URL"], api_key=env["LLM_API_KEY"] or "empty")
    planner = FlowPlanner(catalog, llm, env["LLM_MODEL"], matrix=matrix)
    print(f"plan-only: catalog {len(catalog['apps'])} apps; {len(selected)} task(s)", flush=True)

    def _plan_with_retry(goal: str, attempts: int = 4) -> dict[str, Any]:
        """planner.plan with backoff on transient gateway errors (502/timeout/…).

        PlanValidationError is a real planning outcome — never retried.
        """
        delay, last = 2.0, None
        for i in range(attempts):
            try:
                return planner.plan(goal)
            except PlanValidationError:
                raise
            except Exception as e:  # transient LLM/network — back off and retry
                last = e
                if i < attempts - 1:
                    time.sleep(delay)
                    delay = min(delay * 2, 20.0)
        raise last  # type: ignore[misc]

    report_path = out_root / "plan_report.jsonl"
    rows: list[dict[str, Any]] = []
    with report_path.open("a", encoding="utf-8") as fh:
        for idx, task in selected:
            rec: dict[str, Any] = {
                "id": task["id"], "instruction": task["instruction"],
                "dataset_apps": task.get("apps"), "lang": task.get("lang"),
            }
            try:
                plan = _plan_with_retry(task["instruction"])
                if plan.get("unsatisfiable"):
                    rec.update(status="unsatisfiable", reason=plan.get("reason"))
                else:
                    legs = _plan_legs(plan)
                    rec.update(
                        status="planned", n_legs=len(legs), legs=legs,
                        ra_apps=sorted({leg["app"] for leg in legs if leg["app"]}),
                    )
            except PlanValidationError as e:
                rec.update(status="invalid", reason="; ".join(getattr(e, "errors", []) or [str(e)])[:300])
            except Exception as e:  # network / parse — record, keep going
                rec.update(status="error", reason=f"{type(e).__name__}: {e}"[:300])
            rows.append(rec)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            extra = rec.get("ra_apps") or rec.get("reason", "")
            print(f"[{idx:03d}] {task['id'][:40]:40s} {rec['status']:13s} {extra}", flush=True)

    # ---- aggregate ----
    by_status: dict[str, int] = {}
    app_use: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        for a in r.get("ra_apps") or []:
            app_use[a] = app_use.get(a, 0) + 1
    summary = {
        "mode": "plan_only", "n_tasks": len(rows), "by_status": by_status,
        "ra_app_usage": dict(sorted(app_use.items(), key=lambda kv: -kv[1])),
        "report": str(report_path),
    }
    _write_json(out_root / "plan_summary.json", summary)
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nreport: {report_path}")
    return 0


# ───────────────────────────── mw server lifecycle ─────────────────────────────
def _ensure_mw_server(server_url: str, log_path: Path) -> tuple[subprocess.Popen | None, Any]:
    """Reuse a running MobileWorld server or start one. Returns (proc, log_fh)."""
    if mw_driver._server_health_ok(server_url):
        print(f"Reusing MobileWorld server at {server_url}", flush=True)
        return None, None
    mw_cmd, mw_cwd = mw_driver._resolve_mobileworld_runtime("auto", None)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = log_path.open("ab")
    print(f"Starting MobileWorld server (log={log_path}) ...", flush=True)
    proc = subprocess.Popen([*mw_cmd, "server"], cwd=mw_cwd,
                            stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT)
    if not mw_driver._wait_for_server(server_url):
        proc.send_signal(signal.SIGTERM)
        fh.close()
        raise SystemExit(f"MobileWorld server did not become healthy; see {log_path}")
    print(f"MobileWorld server healthy (pid={proc.pid})", flush=True)
    return proc, fh


# ───────────────────────────── aggregation + report ─────────────────────────────
def _aggregate(rows: list[dict[str, Any]], systems: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sysname in systems:
        srows = [r for r in rows if r["system"] == sysname]
        total = len(srows)
        success = [r for r in srows if r.get("verdict", {}).get("status") == leg_judge.SUCCESS]
        judged = [r for r in srows
                  if r.get("verdict", {}).get("status") in (leg_judge.SUCCESS, leg_judge.FAILURE)]

        def _nums(rs: list[dict[str, Any]], key: str) -> list[float]:
            return [float(r[key]) for r in rs if isinstance(r.get(key), (int, float))]

        # Time/token stats over the COMPLETED tasks only (per the benchmark spec).
        secs = _nums(success, "elapsed_s")
        toks = _nums(success, "total_tokens")
        out[sysname] = {
            "total": total,
            "success": len(success),
            "failure": sum(1 for r in srows if r.get("verdict", {}).get("status") == leg_judge.FAILURE),
            "unjudged": total - len(judged),
            "completion_rate": round(len(success) / total, 3) if total else None,
            "completed_time_s": {
                "mean": round(mean(secs), 1) if secs else None,
                "median": round(median(secs), 1) if secs else None,
            },
            "completed_total_tokens": {
                "mean": round(mean(toks)) if toks else None,
                "median": round(median(toks)) if toks else None,
            },
        }
    return out


def _write_markdown(path: Path, agg: dict[str, Any], systems: list[str],
                    benchmark: str, n_tasks: int) -> None:
    lines = [
        f"# Benchmark A/B — `{benchmark}`",
        "",
        f"- Tasks: **{n_tasks}**  ·  Judge: RelayAgent leg_judge (VLM, uniform)",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "| System | Completion | Success/Total | Mean time (done) | Median time "
        "| Mean tokens (done) | Median tokens |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in systems:
        a = agg.get(s, {})
        cr = a.get("completion_rate")
        t = a.get("completed_time_s", {})
        k = a.get("completed_total_tokens", {})
        lines.append(
            f"| {s} | {f'{cr:.0%}' if cr is not None else '—'} "
            f"| {a.get('success', 0)}/{a.get('total', 0)} "
            f"| {t.get('mean') or '—'}s | {t.get('median') or '—'}s "
            f"| {k.get('mean') or '—'} | {k.get('median') or '—'} |"
        )
    lines += [
        "",
        "> 时间/token 统计仅覆盖**被判完成**的任务。relay token 暂不含 run_plan 的一次性规划调用。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


# ───────────────────────────── main ─────────────────────────────
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--benchmark", default=DEFAULT_BENCHMARK, choices=sorted(BENCHMARKS),
                   help=f"Which task suite to run (default: {DEFAULT_BENCHMARK})")
    p.add_argument("--tasks", type=Path, default=None,
                   help="Override the benchmark's task file (default: the suite's own source)")
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--systems", default="relay,mw",
                   help=f"Comma list of systems, in order. Subset of: {','.join(sorted(SYSTEMS))}")

    sel = p.add_argument_group("task selection")
    sel.add_argument("--all", action="store_true", help="Run the full suite (default: smoke set)")
    sel.add_argument("--per-app", type=int, default=1, help="Smoke tasks per app (default 1)")
    sel.add_argument("--only-id", action="append", default=None, help="Run only these task id(s)")
    sel.add_argument("--limit", type=int, default=None)
    sel.add_argument("--filter-supported", action="store_true",
                     help="Keep only tasks whose apps RelayAgent has a manifest for")
    sel.add_argument("--dry-list", action="store_true", help="List selected tasks and exit")

    run = p.add_argument_group("run knobs")
    run.add_argument("--task-timeout", type=float, default=900.0,
                     help="Seconds before killing one (task,system) run; 0 disables")
    run.add_argument("--step-wait", default=os.getenv("RELAY_STEP_WAIT", "0.5"),
                     help="RelayAgent per-step settle (forwarded to the native runner)")
    run.add_argument("--mw-agent-type", default="general_e2e", help="MobileWorld agent type")
    run.add_argument("--mw-max-round", type=int, default=25, help="MobileWorld max rounds")
    run.add_argument("--server-url", default=mw_driver.DEFAULT_SERVER_URL)
    run.add_argument("--keep-server", action="store_true",
                     help="Do not stop a server this driver started")

    jg = p.add_argument_group("judging")
    jg.add_argument("--no-judge", action="store_true",
                    help="Skip VLM judging; collect raw metrics only")
    jg.add_argument("--judge-model", default=None, help="Override judge model (default LLM_MODEL)")

    p.add_argument("--plan-only", action="store_true",
                   help="Run ONLY RelayAgent's plan/route synthesis per task (no device, "
                        "no mw, no judge); report how each goal is decomposed into a flow")

    args = p.parse_args(argv)

    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    bad = [s for s in systems if s not in SYSTEMS]
    if bad:
        raise SystemExit(f"unknown system(s): {bad}; choose from {sorted(SYSTEMS)}")

    bench = BENCHMARKS[args.benchmark]
    meta, tasks = bench.load(args.tasks)
    selected = _select(
        tasks, bench.smoke,
        only_ids=set(args.only_id) if args.only_id else None,
        run_all=args.all, per_app=args.per_app, limit=args.limit,
        filter_supported=args.filter_supported,
    )
    if not selected:
        raise SystemExit("No tasks selected")

    if args.dry_list:
        for idx, t in selected:
            apps = ",".join(t.get("apps") or [t.get("app", "")])
            print(f"{idx:03d} {t['id'][:40]:40s} [{apps}] :: {t['instruction'][:70]}")
        print(f"\n{len(selected)} task(s) × systems={systems}  (benchmark={args.benchmark})")
        return 0

    try:
        env = ensure_llm_env(ENV_FILE)
    except RuntimeError as e:
        raise SystemExit(str(e)) from e

    if args.plan_only:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_root = (args.out_dir or (TRAJ_LOGS / f"planonly_{args.benchmark}_{ts}")).resolve()
        out_root.mkdir(parents=True, exist_ok=True)
        print(f"plan-only log root: {out_root}", flush=True)
        return _run_plan_only(selected, env, out_root)

    judge_model = args.judge_model or env["LLM_MODEL"]
    llm = None if args.no_judge else OpenAI(base_url=env["LLM_BASE_URL"],
                                            api_key=env["LLM_API_KEY"] or "empty")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = (args.out_dir or (TRAJ_LOGS / f"benchmark_{args.benchmark}_{ts}")).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    _write_json(out_root / "suite.json", {
        "benchmark": args.benchmark, **meta, "systems": systems,
        "selected": [t["id"] for _, t in selected], "judge_model": judge_model,
        "no_judge": args.no_judge, "filter_supported": args.filter_supported, "started_at": ts,
    })
    print(f"A/B log root: {out_root}\nbenchmark={args.benchmark} "
          f"selected={len(selected)} systems={systems}", flush=True)

    timeout_s = None if args.task_timeout == 0 else args.task_timeout
    ctx = RunCtx(
        env=env, timeout_s=timeout_s, mw_prelaunch=bench.mw_prelaunch, step_wait=args.step_wait,
        server_url=args.server_url, mw_agent_type=args.mw_agent_type, mw_max_round=args.mw_max_round,
    )

    server_proc = server_fh = None
    if "mw" in systems:
        server_proc, server_fh = _ensure_mw_server(args.server_url, out_root / "mobileworld_server.log")

    results_path = out_root / "results.jsonl"
    rows: list[dict[str, Any]] = []
    try:
        with results_path.open("a", encoding="utf-8") as results_fh:
            for idx, task in selected:
                task_dir = out_root / f"{idx:03d}_{_slug(task['id'])}"
                task_dir.mkdir(parents=True, exist_ok=True)
                _write_json(task_dir / "task.json", task)
                print(f"\n[{idx:03d}] {task['id']}  apps={task.get('apps')}", flush=True)

                for sysname in systems:
                    sys_dir = task_dir / sysname
                    print(f"   ── {sysname} ──", flush=True)
                    metrics = SYSTEMS[sysname](task, sys_dir, ctx)

                    verdict = {"status": leg_judge.UNKNOWN, "score": -1.0, "reason": "judging skipped"}
                    if not args.no_judge:
                        verdict = _judge(llm, judge_model, task, metrics, task_dir / f"{sysname}_final.png")

                    tok = metrics.get("tokens") or {}
                    row = {
                        "id": task["id"], "app": task.get("app"), "apps": task.get("apps"),
                        "category": task.get("category"), "lang": task.get("lang"),
                        "handoff_required": task.get("handoff_required"), "system": sysname,
                        "returncode": metrics.get("returncode"), "timed_out": metrics.get("timed_out"),
                        "elapsed_s": metrics.get("elapsed_s"),
                        "steps": metrics.get("steps"), "terminal_action": metrics.get("terminal_action"),
                        "prompt_tokens": tok.get("prompt_tokens"),
                        "completion_tokens": tok.get("completion_tokens"),
                        "total_tokens": tok.get("total_tokens"),
                        "relay_legs": metrics.get("relay_legs"), "flow_root": metrics.get("flow_root"),
                        "verdict": verdict,
                    }
                    _write_json(task_dir / f"{sysname}_result.json", row)
                    rows.append(row)
                    results_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    results_fh.flush()
                    print(
                        f"      rc={row['returncode']} timeout={row['timed_out']} "
                        f"elapsed={row['elapsed_s']}s steps={row['steps']} "
                        f"tokens(total)={row['total_tokens']} "
                        f"verdict={verdict['status'].upper()} ({verdict.get('reason', '')[:60]})",
                        flush=True,
                    )
    finally:
        if server_proc is not None and not args.keep_server:
            print("\nStopping MobileWorld server...", flush=True)
            server_proc.send_signal(signal.SIGTERM)
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait()
        if server_fh is not None:
            server_fh.close()

    agg = _aggregate(rows, systems)
    final = {"benchmark": args.benchmark, "out_root": str(out_root),
             "n_tasks": len(selected), "systems": systems, "by_system": agg}
    _write_json(out_root / "summary.json", final)
    _write_markdown(out_root / "summary.md", agg, systems, args.benchmark, len(selected))
    print("\n" + json.dumps(agg, ensure_ascii=False, indent=2))
    print(f"\nsummary: {out_root / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
