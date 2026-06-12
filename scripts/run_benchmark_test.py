#!/usr/bin/env python3
"""One-click A/B benchmark driver: run the SAME tasks through multiple agent systems.

Default benchmark is the **MobileWorld** task suite, pulled from the HuggingFace
dataset ``Tongyi-MAI/MobileWorld`` (``mobileworld_benchmark_task_info.csv``, 201 tasks). Each task is a
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
  - LLM token usage (prompt / completion / total);
  - per-LLM-call records (latency + prompt/completion/cached tokens) under
    ``llm_calls``. For ``mw`` these come from a non-invasive probe
    (``agents.mw_llm_probe``) injected into the mw test subprocess, since
    MobileWorld's own logger persists only the run-level aggregate.

Time/token aggregates are reported over the COMPLETED tasks only.

EXTENSIBILITY — two registries let this grow without surgery:
  * BENCHMARKS  — a benchmark is a task source: how to load + how to pick a smoke
                  subset (``--benchmark``). Tasks are normalized to the contract in
                  ``_normalize_task`` so every system + the judge consume them the
                  same way. ``mobileworld`` + ``androiddaily`` (HF) and ``relaybench``
                  (local yaml) ship.
  * SYSTEMS     — a system is one agent runner ``(task, sys_dir, ctx) -> metrics``
                  (``--systems``). Add an entry to A/B a third agent.

NOTE on coverage: the MobileWorld apps (Mail/Messages/Mastodon/Calendar/Files/…)
mostly have no RelayAgent manifest and may not be installed on a real device; only
Maps/MCP-Amap overlaps (高德). Expect many tasks to fail for BOTH systems on a real
device — that is an honest, same-device, same-judge comparison. ``--filter-supported``
trims to the tasks whose apps RelayAgent can actually attempt.

Relay tokens are read from run_plan's authoritative ``<flow_root>/token_usage.json``
(``total`` includes the plan-synthesis phase; ``by_phase`` splits plan/flow/agent).
Per-call latency+tokens land in each row's ``llm_calls``: for ``relay`` from that
file, for ``mw`` from a non-invasive probe (``agents.mw_llm_probe``). Full per-call
text (messages/response) is persisted on disk for both — relay in each leg's
``traj.json``, mw in ``<sys>/user_task/llm_calls.json`` — not in the lean results row.

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
    uv run python scripts/run_benchmark_test.py --benchmark relaybench
    uv run python scripts/run_benchmark_test.py --benchmark androiddaily --plan-only --all
    uv run python scripts/run_benchmark_test.py --skip-mcp --plan-only --all      # MobileWorld w/o MCP
    uv run python scripts/run_benchmark_test.py --benchmark relaybench --systems relay
    uv run python scripts/run_benchmark_test.py --only-id AddBusinessTripAskUserTask
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
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
from agents._adb import (  # noqa: E402
    force_stop,
    foreground_package,
    keyevent,
    kill_all_apps,
    reset_airplane_mode,
)
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
MW_TASK_INFO = REPO_ROOT / "benchmark" / "mobileworld_benchmark_task_info.csv"
RELAYBENCH_TASKS = REPO_ROOT / "benchmark" / "relaybench_tasks.yaml"
AD_DATASET = "stepfun-ai/AndroidDaily"
AD_TASK_INFO = REPO_ROOT / "benchmark" / "androiddaily_task_info.csv"

# MobileWorld app name -> RelayAgent manifest app id. Used by --filter-supported
# and for display; extend this as RelayAgent gains manifests for more apps.
MW_APP_TO_RA = {
    "Maps": "com.autonavi.minimap",
    "MCP-Amap": "com.autonavi.minimap",
}

# Token-throughput model constants for re-pricing per-call LLM time, so each
# results.jsonl row carries a gateway-queue-free wall-clock the moment it lands
# (instead of only after a batch normalize pass). Same formula/constants as
# scripts/normalize_wall_clock.py + phaseB_summary.py:
#   model_time = gamma + alpha*(prompt-cached) + beta*completion
# Default file is the calibrated/rounded fit dropped under traj_logs/phaseB.
NORM_FIT_FILE = TRAJ_LOGS / "phaseB" / "wall_norm_rounded.json"


def _load_norm_const(fit_path: Path) -> tuple[float, float, float] | None:
    """(gamma, alpha, beta) from a fit file, or None if missing/malformed."""
    try:
        fit = json.loads(fit_path.read_text(encoding="utf-8"))
        return (float(fit.get("gamma_s_per_call") or 0.0),
                float(fit["alpha_s_per_prefill_tok"]),
                float(fit["beta_s_per_decode_tok"]))
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


def _norm_llm_time(llm_calls: list[dict[str, Any]] | None,
                   const: tuple[float, float, float] | None,
                   elapsed_s: float | None) -> dict[str, float | None]:
    """Per-case LLM-time accounting from this row's own ``llm_calls``.

    Returns ``llm_time_actual_s`` (measured, queue-tainted), ``llm_time_norm_s``
    (re-priced by the token model), and ``elapsed_s_norm`` (wall-clock with the
    LLM portion swapped). Column names AND call-selection mirror
    normalize_wall_clock.py / phaseB_summary._usable_calls so the live value and
    any later batch pass agree to the digit: only calls carrying BOTH a latency
    and prompt+completion tokens contribute (the rest stay in the device
    remainder). const=None (no fit file) → actual only, norm fields left None."""
    usable: list[tuple[float, int, int, int]] = []
    for c in llm_calls or []:
        if c.get("error"):
            continue
        v = c.get("elapsed_s")
        v = c.get("latency_s") if v is None else v
        p, comp = c.get("prompt_tokens"), c.get("completion_tokens")
        if v is None or p is None or comp is None:
            continue
        usable.append((float(v), int(p), int(comp), int(c.get("cached_tokens") or 0)))
    actual = sum(t for t, _, _, _ in usable)
    out: dict[str, float | None] = {
        "llm_time_actual_s": round(actual, 3),
        "llm_time_norm_s": None, "elapsed_s_norm": None,
    }
    if const is None:
        return out
    gamma, alpha, beta = const
    norm = sum(gamma + alpha * (p - cached) + beta * comp for _, p, comp, cached in usable)
    out["llm_time_norm_s"] = round(norm, 3)
    if elapsed_s is not None:
        wall = float(elapsed_s) - actual + norm
        out["elapsed_s_norm"] = round(norm if wall < 0 else wall, 3)
    return out


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
        if MW_TASK_INFO.is_file():
            path = MW_TASK_INFO
        else:
            try:
                from huggingface_hub import hf_hub_download
            except ImportError as e:
                raise SystemExit("huggingface_hub is required to load the MobileWorld dataset") from e
            hf_path = Path(hf_hub_download(MW_DATASET, "task_info.csv", repo_type="dataset"))
            MW_TASK_INFO.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(hf_path, MW_TASK_INFO)
            path = MW_TASK_INFO
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



# ---- benchmark: relaybench (balanced single + cross-app yaml) ----
def _load_relaybench(path: Path | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = path or RELAYBENCH_TASKS
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = doc.get("tasks") or []
    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"No tasks found in {path}")
    meta = {
        "suite": doc.get("suite"),
        "version": doc.get("version"),
        "total_tasks": doc.get("total_tasks"),
        "single_app_tasks": doc.get("single_app_tasks"),
        "cross_app_tasks": doc.get("cross_app_tasks"),
    }
    tasks = []
    for t in raw:
        caps = t.get("capabilities") or []
        if caps and not t.get("capability"):
            t = {**t, "capability": caps[0]}
        tasks.append(_normalize_task(t))
    return meta, tasks


def _smoke_relaybench(tasks: list[dict[str, Any]], per_app: int) -> list[int]:
    """One task per manifest app; prefer single-app, easy, non-handoff."""

    def rank(t: dict[str, Any]) -> tuple[int, int, int]:
        single = 0 if t.get("category") != "cross_app" and len(t.get("apps") or []) <= 1 else 1
        ro = 0 if (t.get("category") == "info_qa" and not t.get("handoff_required")) else 1
        easy = {"easy": 0, "medium": 1, "hard": 2}.get(t.get("difficulty"), 3)
        return (single, ro, easy)

    return _smoke_by_app(tasks, per_app, rank)


# ---- benchmark: androiddaily (HuggingFace stepfun-ai/AndroidDaily) ----
# Chinese daily-app suite; columns are Chinese. AndroidDaily's native metric is
# step-action-accuracy against logged ground-truth traces — RelayAgent routes to
# in-app agents and produces no comparable step sequence, so we reuse only its
# task INSTRUCTIONS and score both systems with the uniform e2e VLM judge.
_AD_HANDOFF_SCENES = {"出行交通", "购物消费", "生活服务", "本地生活"}


def _load_androiddaily(path: Path | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if path is None:
        if AD_TASK_INFO.is_file():
            path = AD_TASK_INFO
        else:
            try:
                from huggingface_hub import hf_hub_download
            except ImportError as e:
                raise SystemExit("huggingface_hub is required to load the AndroidDaily dataset") from e
            hf_path = Path(hf_hub_download(AD_DATASET, "Android Daily.csv", repo_type="dataset"))
            AD_TASK_INFO.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(hf_path, AD_TASK_INFO)
            path = AD_TASK_INFO
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    if not rows:
        raise SystemExit(f"No tasks in {path}")
    tasks: list[dict[str, Any]] = []
    for i, r in enumerate(rows, 1):
        instruction = (r.get("任务") or "").strip()
        if not instruction:
            continue
        app = (r.get("APP名称") or "").strip()
        scene = (r.get("场景") or "").strip()
        tasks.append(_normalize_task({
            "id": f"AD-{i:03d}",
            "instruction": instruction,
            "apps": [app] if app else [],
            "category": scene,
            "difficulty": (r.get("综合难度") or "").strip() or None,
            "handoff_required": scene in _AD_HANDOFF_SCENES,
            "lang": "cn",
            "tags": [t for t in [r.get("task_tag"), r.get("信息处理类型")] if t],
        }))
    meta = {"suite": "AndroidDaily", "source": AD_DATASET, "n_tasks": len(tasks)}
    return meta, tasks


def _smoke_androiddaily(tasks: list[dict[str, Any]], per_app: int) -> list[int]:
    """Per app, prefer easy atomic tasks for a quick representative subset."""
    def rank(t: dict[str, Any]) -> tuple[int, int]:
        return ({"easy": 0, "medium": 1, "hard": 2}.get(t.get("difficulty"), 3), 0)

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
    "relaybench": Benchmark("relaybench", _load_relaybench, _smoke_relaybench, mw_prelaunch=False),
    "androiddaily": Benchmark("androiddaily", _load_androiddaily, _smoke_androiddaily, mw_prelaunch=False),
}
DEFAULT_BENCHMARK = "mobileworld"


_CATALOG_APP_IDS: frozenset[str] | None = None


def _catalog_app_ids() -> frozenset[str]:
    global _CATALOG_APP_IDS
    if _CATALOG_APP_IDS is None:
        from agents.card_catalog import build_catalog
        _CATALOG_APP_IDS = frozenset(a["app_id"] for a in build_catalog()["apps"])
    return _CATALOG_APP_IDS


def _supported(task: dict[str, Any]) -> bool:
    """True if RelayAgent has a manifest for every app the task touches."""
    apps = task.get("apps") or []
    if not apps:
        return False
    catalog = _catalog_app_ids()
    if all(a in catalog for a in apps):
        return True
    return all(a in MW_APP_TO_RA for a in apps)


def _touches_mcp(task: dict[str, Any]) -> bool:
    """True if any app the task touches is an MCP-* tool source (not a real GUI app)."""
    return any((a or "").startswith("MCP-") for a in (task.get("apps") or []))


def _select(
    tasks: list[dict[str, Any]],
    smoke: Callable[[list[dict[str, Any]], int], list[int]],
    *,
    only_ids: set[str] | None,
    run_all: bool,
    per_app: int,
    limit: int | None,
    filter_supported: bool,
    skip_mcp: bool = False,
) -> list[tuple[int, dict[str, Any]]]:
    if only_ids:
        indexed = [(i, t) for i, t in enumerate(tasks, 1) if t.get("id") in only_ids]
    elif run_all:
        indexed = list(enumerate(tasks, 1))
    else:
        keep = set(smoke(tasks, per_app))
        indexed = [(i, t) for i, t in enumerate(tasks, 1) if i in keep]
    if skip_mcp:
        indexed = [(i, t) for i, t in indexed if not _touches_mcp(t)]
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


def _reset_device() -> None:
    """Clean the device between A/B systems so neither inherits the other's
    leftover screen (a system that runs *after* another would otherwise read the
    previous one's result/answer surface and "succeed"/finish in ~1 step). We
    force-stop whatever app is foregrounded — package-agnostic, so it works for
    AndroidDaily where the task `app` is a Chinese label, not a package — then
    press HOME so the next system starts from the launcher and must navigate
    itself. Best-effort: failures only warn, never abort the run."""
    try:
        pkg = foreground_package()
        if pkg and pkg not in ("com.android.systemui", "com.android.launcher",
                               "com.google.android.apps.nexuslauncher"):
            force_stop(pkg)
        keyevent("KEYCODE_HOME")
        # a task may leave airplane mode ON (e.g. OpenFlightModeTask), which cuts
        # network for every task after it — turn it back off here
        if reset_airplane_mode():
            print("      [reset] airplane mode left ON — disabled", flush=True)
    except (subprocess.TimeoutExpired, OSError) as exc:  # pragma: no cover
        print(f"      [reset] device reset skipped: {exc}", flush=True)


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
        # Per-call latency + prompt/completion tokens, written beside traj.json by the
        # non-invasive probe (agents.mw_llm_probe) injected into the mw test subprocess.
        "--llm-calls-out", str(sys_dir / "user_task" / "llm_calls.json"),
    ]
    if ctx.mw_prelaunch and task.get("app"):
        cmd += ["--app", task["app"]]
    else:
        cmd += ["--no-prelaunch"]
    rc, timed_out, elapsed = _run_subprocess(cmd, sys_dir, env=None, timeout_s=ctx.timeout_s)

    # MobileWorld's TrajLogger writes <sys_dir>/user_task/traj.json with aggregate
    # token_usage; the probe writes per-call records (latency + token split) to
    # llm_calls.json. token_usage falls back to the probe's summed total.
    traj = _read_json(sys_dir / "user_task" / "traj.json") or {}
    node = traj.get("0") if isinstance(traj.get("0"), dict) else {}
    calls_doc = _read_json(sys_dir / "user_task" / "llm_calls.json") or {}
    # Full per-call text (messages/response) stays in llm_calls.json on disk; the
    # results.jsonl row carries only the per-call METRICS (kept symmetric with
    # relay's token_usage.json projection, and keeps the appended jsonl small).
    _METRIC_KEYS = ("index", "purpose", "model", "elapsed_s", "ok",
                    "prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens")
    llm_calls = [{k: c.get(k) for k in _METRIC_KEYS} for c in (calls_doc.get("llm_calls") or [])]
    tokens = node.get("token_usage") or traj.get("token_usage") or calls_doc.get("total") or {}
    steps_list = node.get("traj") if isinstance(node.get("traj"), list) else []
    terminal_action = None
    if steps_list:
        terminal_action = (steps_list[-1].get("action") or {}).get("action_type")
    return {
        "returncode": rc, "timed_out": timed_out, "elapsed_s": elapsed,
        "tokens": _norm_tokens(tokens), "steps": len(steps_list) or None,
        "terminal_action": terminal_action,
        "llm_calls": llm_calls,
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
    """Per-leg verdicts/reply/steps + the whole-run token accounting.

    Tokens come from run_plan's authoritative ``<flow_root>/token_usage.json`` when
    present: its ``total`` includes the **plan-synthesis** phase (which per-leg
    summaries omit — the old undercount), ``by_phase`` splits plan/flow/agent, and
    ``calls`` carries per-call latency+tokens (parity with the mw probe). Only when
    that file is missing do we fall back to summing per-leg summary + flow_llm_calls.
    """
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

    # Authoritative whole-run accounting (includes the plan-synthesis phase).
    tu = _read_json(flow_root / "token_usage.json") or {}
    if tu.get("total"):
        tokens = _norm_tokens(tu["total"])
    else:  # fallback: per-leg sum (undercounts plan-synthesis — logged below)
        tokens = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}
        print(f"      [warn] no token_usage.json in {flow_root.name}; "
              f"relay tokens undercount the plan-synthesis phase", flush=True)
    return {
        "tokens": tokens,
        "token_by_phase": tu.get("by_phase"),
        "llm_calls": tu.get("calls") or [],
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
        "llm_calls": harvested.get("llm_calls"),          # per-call latency+tokens (parity with mw)
        "token_by_phase": harvested.get("token_by_phase"),  # plan / flow / agent split
    }


SYSTEMS: dict[str, Callable[[dict[str, Any], Path, RunCtx], dict[str, Any]]] = {
    "relay": _run_relay,
    "mw": _run_mw,
}


# ───────────────────────────── uniform VLM judge ─────────────────────────────
def _capture_final(final_png: Path):
    """Snap + persist the post-run screen. Always run (even with --no-judge) so a
    human can eyeball <sys>_final.png later via manual_judge.py. Returns the PIL
    image (or None) for an optional in-line LLM verdict."""
    shot = adb_screencap()
    if shot is not None:
        try:
            shot.save(final_png)
        except OSError:
            pass
    return shot


def _judge(llm: OpenAI, model: str, task: dict[str, Any], metrics: dict[str, Any],
           final_png: Path, shot=None) -> dict[str, Any]:
    """Score one finished run with leg_judge against a post-run screenshot.

    The goal handed to the judge is the user instruction (PLUS the task's explicit
    `success` rubric when the benchmark provides one), so both systems are held to
    the same definition of done. A run the judge calls `loading` is re-shot once
    and re-judged (settle retry). `shot` may be a pre-captured frame (from
    _capture_final); when None it is captured here.
    """
    goal = task["instruction"]
    if task.get("success"):
        goal = f"{goal}\n\n[完成判定标准] {task['success']}"

    if shot is None:
        shot = _capture_final(final_png)

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
def _leg_kind(step: dict[str, Any]) -> str | None:
    """Classify one plan step as a leg kind, or None if it is not a leg.

    `ask_user` steps are control flow, not legs. A `mobileworld` step is an MW
    fallback leg. An app step on the generic foundation_llm route is
    `foundation`; on a real vertical capability it is `specialized`.
    """
    if not isinstance(step, dict):
        return None
    stype = step.get("type")
    if stype == "ask_user":
        return None
    if stype == "mobileworld":  # agents.flow_planner.MW_STEP_TYPE
        return "mw"
    cap = step.get("capability")
    if cap == "foundation_llm":
        return "foundation"
    if cap or step.get("app"):
        return "specialized"
    return None


def _plan_legs(plan: dict[str, Any]) -> list[dict[str, str]]:
    """Legs of a synthesized plan, in order, each tagged with its kind.

    Kinds: `specialized` (real vertical capability), `foundation`
    (foundation_llm generic route), `mw` (MobileWorld fallback leg).
    """
    legs: list[dict[str, str]] = []
    for step in plan.get("steps") or []:
        kind = _leg_kind(step)
        if kind is None:
            continue
        legs.append({
            "app": step.get("app", ""),
            "capability": step.get("capability", ""),
            "kind": kind,
        })
    return legs


def _plan_tier(legs: list[dict[str, str]]) -> str:
    """Whole-plan tier from its leg kinds.

    - `covered`: every leg routes to a specialized in-app capability.
    - `mw`: every leg is a MobileWorld fallback leg.
    - `mixed`: some legs are MW fallback, some are not.
    - `foundation_fallback`: no MW leg, but at least one generic foundation_llm
      leg (i.e. leans on a chat assistant rather than a vertical capability).
    """
    kinds = [l["kind"] for l in legs]
    if not kinds:
        return "foundation_fallback"
    has_mw = "mw" in kinds
    has_non_mw = any(k != "mw" for k in kinds)
    if has_mw and has_non_mw:
        return "mixed"
    if has_mw:
        return "mw"
    if all(k == "specialized" for k in kinds):
        return "covered"
    return "foundation_fallback"


def _plan_only_aggregate(rows: list[dict[str, Any]], report_path: Path | str) -> dict[str, Any]:
    """Aggregate plan-only rows into the plan_summary schema. Reusable so a merge
    (old-covered rows + a rerun of the rest) can re-derive the summary identically."""
    by_status: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    app_use: dict[str, int] = {}
    spec_app_hits: dict[str, int] = {}
    spec_cap_hits: dict[str, int] = {}
    total_legs = 0
    total_mw_legs = 0
    mixed_mw_ratios: dict[str, float] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        by_tier[r.get("tier", "?")] = by_tier.get(r.get("tier", "?"), 0) + 1
        for a in r.get("ra_apps") or []:
            app_use[a] = app_use.get(a, 0) + 1
        for a in r.get("spec_apps") or []:
            spec_app_hits[a] = spec_app_hits.get(a, 0) + 1
        for c in r.get("spec_caps") or []:
            spec_cap_hits[c] = spec_cap_hits.get(c, 0) + 1
        total_legs += r.get("n_legs", 0)
        total_mw_legs += r.get("n_mw_legs", 0)
        if r.get("tier") == "mixed":
            mixed_mw_ratios[r["id"]] = r.get("mw_ratio", 0.0)
    n = len(rows) or 1
    covered = by_tier.get("covered", 0)
    mw_tasks = by_tier.get("mw", 0)
    mixed_tasks = by_tier.get("mixed", 0)
    return {
        "mode": "plan_only", "n_tasks": len(rows),
        "by_status": by_status,
        # Leg-kind tiers: covered = every leg routes to a specialized in-app
        # capability; foundation_fallback = no MW leg but ≥1 generic foundation_llm
        # leg; mw = every leg is a MobileWorld fallback (== baseline substrate);
        # mixed = some MW legs + some non-MW legs; invalid/error.
        "by_tier": dict(sorted(by_tier.items(), key=lambda kv: -kv[1])),
        "covered_rate": round(covered / n, 3),
        # MobileWorld fallback proportion, at both task and leg granularity.
        "mw_fallback": {
            "tasks_fully_mw": mw_tasks,
            "tasks_mixed": mixed_tasks,
            "tasks_touching_mw": mw_tasks + mixed_tasks,
            "task_touch_rate": round((mw_tasks + mixed_tasks) / n, 3),
            "total_legs": total_legs,
            "total_mw_legs": total_mw_legs,
            "mw_leg_rate": round(total_mw_legs / total_legs, 3) if total_legs else 0.0,
            "mixed_task_mw_ratios": dict(sorted(mixed_mw_ratios.items(), key=lambda kv: -kv[1])),
        },
        "covered_app_hits": dict(sorted(spec_app_hits.items(), key=lambda kv: -kv[1])),
        "covered_capability_hits": dict(sorted(spec_cap_hits.items(), key=lambda kv: -kv[1])),
        "ra_app_usage": dict(sorted(app_use.items(), key=lambda kv: -kv[1])),
        "report": str(report_path),
    }


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
                    # MW fallback off: the whole request would run on MobileWorld
                    # (general_e2e) at runtime — i.e. RA reduces to the baseline
                    # substrate. Count it as one fully-MW plan.
                    rec.update(status="unsatisfiable", tier="mw",
                               n_legs=0, legs=[], n_mw_legs=0, mw_ratio=1.0,
                               reason=plan.get("reason"))
                else:
                    legs = _plan_legs(plan)
                    spec = [l for l in legs if l["kind"] == "specialized"]
                    n_mw = sum(1 for l in legs if l["kind"] == "mw")
                    rec.update(
                        status="planned",
                        tier=_plan_tier(legs),
                        n_legs=len(legs), legs=legs,
                        n_mw_legs=n_mw,
                        mw_ratio=round(n_mw / len(legs), 3) if legs else 0.0,
                        ra_apps=sorted({leg["app"] for leg in legs if leg["app"]}),
                        spec_apps=sorted({l["app"] for l in spec if l["app"]}),
                        spec_caps=sorted({l["capability"] for l in spec}),
                    )
            except PlanValidationError as e:
                rec.update(status="invalid", tier="invalid",
                           reason="; ".join(getattr(e, "errors", []) or [str(e)])[:300])
            except Exception as e:  # network / parse — record, keep going
                rec.update(status="error", tier="error", reason=f"{type(e).__name__}: {e}"[:300])
            rows.append(rec)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            extra = rec.get("ra_apps") or rec.get("reason", "")
            print(f"[{idx:03d}] {task['id'][:40]:40s} {rec['status']:13s} {extra}", flush=True)

    # ---- aggregate ----
    summary = _plan_only_aggregate(rows, report_path)
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


def _aggregate_by_app(rows: list[dict[str, Any]], systems: list[str]) -> dict[str, Any]:
    """Per-app × per-system metrics (group by task['app']) for the per-app figure.

    success_rate is over that app's tasks; time/tokens over its COMPLETED tasks
    (same completed-only convention as ``_aggregate``). Feeds the dumbbell
    (fig4) — RA wins concentrate on its manifest apps; on the rest RA ≈ baseline.
    """
    out: dict[str, Any] = {}
    for app in sorted({(r.get("app") or "?") for r in rows}):
        per_sys: dict[str, Any] = {}
        for sysname in systems:
            srows = [r for r in rows if r["system"] == sysname and (r.get("app") or "?") == app]
            total = len(srows)
            success = [r for r in srows if r.get("verdict", {}).get("status") == leg_judge.SUCCESS]
            secs = [float(r["elapsed_s"]) for r in success if isinstance(r.get("elapsed_s"), (int, float))]
            toks = [float(r["total_tokens"]) for r in success if isinstance(r.get("total_tokens"), (int, float))]
            per_sys[sysname] = {
                "n": total,
                "success_rate": round(len(success) / total, 3) if total else None,
                "completed_time_s": {
                    "mean": round(mean(secs), 1) if secs else None,
                    "median": round(median(secs), 1) if secs else None},
                "completed_total_tokens": {
                    "mean": round(mean(toks)) if toks else None,
                    "median": round(median(toks)) if toks else None},
            }
        out[app] = per_sys
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
    sel.add_argument("--ids-file", type=Path, default=None,
                     help="Run only the task ids listed in this file (one per line; "
                          "'#' comments and blanks ignored). Unions with --only-id.")
    sel.add_argument("--limit", type=int, default=None)
    sel.add_argument("--filter-supported", action="store_true",
                     help="Keep only tasks whose apps RelayAgent has a manifest for")
    sel.add_argument("--skip-mcp", action="store_true",
                     help="Drop tasks touching an MCP-* tool source (MobileWorld: 40 -> 161 kept)")
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
    run.add_argument("--route-overlay", action="store_true",
                     help="Keep the route-solidification overlay ON (default OFF: every task "
                          "routes through the real planner, no 0-LLM table-lookup short-circuit, "
                          "no cross-task cache leakage — clean per-task measurement)")
    run.add_argument("--step-log", action="store_true",
                     help="Keep RelayAgent per-step screenshot logging ON (default OFF: the "
                          "per-step PNG + marked-frame re-encode is real per-step cost that "
                          "pollutes wall-clock; traj.json action trace is kept either way)")
    run.add_argument("--full-reply", action="store_true",
                     help="Keep RelayAgent full-reply scroll-capture ON (default OFF for "
                          "fairness vs the MobileWorld baseline, which has no scroll-to-capture "
                          "and only reads the on-screen reply: wait_for_reply stops at "
                          "'screen stable' and returns the first visible frame's text)")
    run.add_argument("--no-device-reset", action="store_true",
                     help="Do NOT force-stop the foreground app + HOME before each system "
                          "(default: reset, so mw --no-prelaunch can't inherit relay's "
                          "leftover result screen and finish in ~1 step)")

    jg = p.add_argument_group("judging")
    jg.add_argument("--no-judge", action="store_true",
                    help="Skip VLM judging; collect raw metrics only")
    jg.add_argument("--judge-model", default=None, help="Override judge model (default LLM_MODEL)")

    p.add_argument("--plan-only", action="store_true",
                   help="Run ONLY RelayAgent's plan/route synthesis per task (no device, "
                        "no mw, no judge); report how each goal is decomposed into a flow")

    args = p.parse_args(argv)

    # Route-solidification overlay OFF by default for benchmarking: it would let
    # later tasks short-circuit the planner via 0-LLM table lookups, leaking warm
    # state across tasks and making token/time order-dependent + unfair. The
    # in-process plan-only planner and every relay subprocess (which inherits
    # os.environ) both honor this. Re-enable with --route-overlay for an ablation.
    if not args.route_overlay:
        os.environ["RELAY_ROUTE_OVERLAY"] = "0"
    # Per-step screenshot logging OFF by default: it writes a PNG (+ marked frame
    # for tap/swipe) every step — real wall-clock cost that biases the timing
    # metric. The relay subprocess inherits os.environ; traj.json is unaffected.
    if not args.step_log:
        os.environ["RELAY_STEP_LOG"] = "0"
    # Full-reply scroll-capture OFF by default for fairness: the MobileWorld
    # baseline (general_e2e) has no scroll-to-capture — it reads the on-screen
    # reply and `answer`s. Letting RelayAgent scroll offscreen reply chunks into
    # view would give it strictly more reply content for the same goal. Off ⇒
    # wait_for_reply stops at "screen stable". Re-enable with --full-reply.
    if not args.full_reply:
        os.environ["RELAY_CAPTURE_FULL_REPLY"] = "0"

    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    bad = [s for s in systems if s not in SYSTEMS]
    if bad:
        raise SystemExit(f"unknown system(s): {bad}; choose from {sorted(SYSTEMS)}")

    bench = BENCHMARKS[args.benchmark]
    meta, tasks = bench.load(args.tasks)
    only_ids: set[str] = set(args.only_id) if args.only_id else set()
    if args.ids_file:
        only_ids |= {ln.strip() for ln in args.ids_file.read_text(encoding="utf-8").splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#")}
    selected = _select(
        tasks, bench.smoke,
        only_ids=only_ids or None,
        run_all=args.all, per_app=args.per_app, limit=args.limit,
        filter_supported=args.filter_supported, skip_mcp=args.skip_mcp,
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
    norm_const = _load_norm_const(NORM_FIT_FILE)
    if norm_const is None:
        print(f"   (no norm fit at {NORM_FIT_FILE} — rows get llm_time_actual_s only)",
              flush=True)
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
                    # clean slate per system: no inheriting the prior system's
                    # leftover screen (relay cold-launches its own apps anyway;
                    # this matters most for mw --no-prelaunch, which would else
                    # read relay's result surface and finish in ~1 step).
                    if not args.no_device_reset:
                        _reset_device()
                    metrics = SYSTEMS[sysname](task, sys_dir, ctx)

                    # Always snap the final frame (manual_judge.py reads it);
                    # only the LLM verdict is gated by --no-judge.
                    final_png = task_dir / f"{sysname}_final.png"
                    shot = _capture_final(final_png)
                    verdict = {"status": leg_judge.UNKNOWN, "score": -1.0,
                               "reason": "judging skipped — manual"}
                    if not args.no_judge:
                        verdict = _judge(llm, judge_model, task, metrics, final_png, shot=shot)

                    tok = metrics.get("tokens") or {}
                    # re-price this case's LLM time off its own per-call records,
                    # so the row lands with a queue-free wall-clock immediately
                    norm = _norm_llm_time(metrics.get("llm_calls"), norm_const,
                                          metrics.get("elapsed_s"))
                    row = {
                        "id": task["id"], "app": task.get("app"), "apps": task.get("apps"),
                        "category": task.get("category"), "lang": task.get("lang"),
                        "handoff_required": task.get("handoff_required"), "system": sysname,
                        "returncode": metrics.get("returncode"), "timed_out": metrics.get("timed_out"),
                        "elapsed_s": metrics.get("elapsed_s"),
                        "llm_time_actual_s": norm["llm_time_actual_s"],
                        "llm_time_norm_s": norm["llm_time_norm_s"],
                        "elapsed_s_norm": norm["elapsed_s_norm"],
                        "steps": metrics.get("steps"), "terminal_action": metrics.get("terminal_action"),
                        "prompt_tokens": tok.get("prompt_tokens"),
                        "completion_tokens": tok.get("completion_tokens"),
                        "total_tokens": tok.get("total_tokens"),
                        "llm_calls": metrics.get("llm_calls"),  # per-call: mw probe / relay token_usage.json
                        "token_by_phase": metrics.get("token_by_phase"),  # relay: plan/flow/agent split
                        "relay_legs": metrics.get("relay_legs"), "flow_root": metrics.get("flow_root"),
                        "verdict": verdict,
                    }
                    _write_json(task_dir / f"{sysname}_result.json", row)
                    rows.append(row)
                    results_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    results_fh.flush()
                    _norm_s = row["elapsed_s_norm"]
                    print(
                        f"      rc={row['returncode']} timeout={row['timed_out']} "
                        f"elapsed={row['elapsed_s']}s"
                        f"{f' norm={_norm_s}s' if _norm_s is not None else ''} "
                        f"steps={row['steps']} "
                        f"tokens(total)={row['total_tokens']} "
                        f"verdict={verdict['status'].upper()} ({verdict.get('reason', '')[:60]})",
                        flush=True,
                    )

                # between-task hard reset: force-stop every running app so no
                # state (chat thread, half-finished flow, session sheet) survives
                # into the next task. Shares the --no-device-reset gate with the
                # per-system reset above.
                if not args.no_device_reset:
                    try:
                        kill_all_apps()
                    except Exception as exc:  # best-effort; never abort the suite
                        print(f"      [kill-all] skipped: {exc}", flush=True)
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
             "n_tasks": len(selected), "systems": systems, "by_system": agg,
             "by_app": _aggregate_by_app(rows, systems)}
    _write_json(out_root / "summary.json", final)
    _write_markdown(out_root / "summary.md", agg, systems, args.benchmark, len(selected))
    print("\n" + json.dumps(agg, ensure_ascii=False, indent=2))
    print(f"\nsummary: {out_root / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
