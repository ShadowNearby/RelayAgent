#!/usr/bin/env python3
"""Run benchmark/single_app_tasks.yaml with per-task logs.

Each task is dispatched as one `run_native.py` run pinned to the
benchmark's app and capability. Logs are isolated under:

    traj_logs/single_app_benchmark_<timestamp>/<NN>_<task-id>/

The runner writes:
  - user_task/traj.json      trajectory
  - user_task/reply.json     RelayAgent captured reply
  - user_task/wall_clock.json
  - user_task/steps/         per-step screenshots + action + click pos
                             (RELAY_STEP_LOG=0 to disable; see CLAUDE.md)
  - stdout.log / stderr.log  subprocess output
  - task.json                task metadata
  - run.json                 exit code and timing

The top-level summary.jsonl is appended after every task so interrupted runs
still leave a useful audit trail.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agents.runtime_config import ensure_llm_env  # noqa: E402

ENV_FILE = REPO_ROOT / ".env"
# Each task is one direct-adb run_native.py subprocess.
RUN_NATIVE = REPO_ROOT / "scripts" / "run_native.py"
DEFAULT_TASKS = REPO_ROOT / "benchmark" / "single_app_tasks.yaml"


def _slug(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", s).strip("_") or "task"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_tasks(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tasks = doc.get("tasks") or []
    if not isinstance(tasks, list) or not tasks:
        raise SystemExit(f"No tasks found in {path}")
    return doc, tasks


def _select_tasks(
    tasks: list[dict[str, Any]],
    only_ids: set[str] | None,
    start_after: str | None,
    limit: int | None,
) -> list[tuple[int, dict[str, Any]]]:
    indexed = list(enumerate(tasks, 1))
    if only_ids:
        indexed = [(i, t) for i, t in indexed if t.get("id") in only_ids]
    if start_after:
        seen = False
        out: list[tuple[int, dict[str, Any]]] = []
        for i, t in indexed:
            if seen:
                out.append((i, t))
            elif t.get("id") == start_after:
                seen = True
        indexed = out
    if limit is not None:
        indexed = indexed[:limit]
    return indexed


def _build_cmd(
    task: dict[str, Any],
    extra_args: list[str],
) -> list[str]:
    # run_native reads LLM_* + RELAY_* from the child env; app+goal positional.
    cmd = [
        sys.executable,
        str(RUN_NATIVE),
        task["app"],
        task["instruction"],
    ]
    if not any(a == "--step_wait_time" or a.startswith("--step_wait_time=") for a in extra_args):
        cmd += ["--step_wait_time", os.getenv("RELAY_STEP_WAIT", "0.2")]
    cmd += extra_args
    return cmd


def _task_env(task: dict[str, Any], env: dict[str, str], task_dir: Path) -> dict[str, str]:
    user_task_dir = task_dir / "user_task"
    user_task_dir.mkdir(parents=True, exist_ok=True)
    return {
        **env,
        **os.environ,
        "RELAY_TARGET_APP": task["app"],
        "RELAY_SKIP_OPEN_APP": "1",
        "RELAY_AGENT_LAUNCH": "1",
        "RELAY_FORCE_CAPABILITY": task["capability"],
        "RELAY_INVOCATION_TEXT": task["instruction"],
        "RELAY_REPLY_OUT": str(user_task_dir / "reply.json"),
        "RELAY_WALL_OUT": str(user_task_dir / "wall_clock.json"),
        # Route per-step logs (screenshots + action + click pos) into this
        # task's dir; otherwise run_native's StepLogger defaults to the global
        # traj_logs/user_task/steps and each task's steps get rotated out by the
        # next task's _rotate_traj_dir. RELAY_STEP_LOG=0 (in env) still disables.
        "RELAY_STEP_LOG_DIR": str(user_task_dir),
        "RELAY_WAIT_SECONDS": os.getenv("RELAY_WAIT_SECONDS", "0.2"),
    }


def _run_one(
    idx: int,
    task: dict[str, Any],
    env: dict[str, str],
    out_root: Path,
    extra_args: list[str],
    timeout_s: float | None,
) -> dict[str, Any]:
    task_dir = out_root / f"{idx:02d}_{_slug(task['id'])}"
    task_dir.mkdir(parents=True, exist_ok=True)
    _write_json(task_dir / "task.json", task)

    child_env = _task_env(task, env, task_dir)
    cmd = _build_cmd(task, extra_args)
    _write_json(task_dir / "command.json", {"cmd": cmd, "env": {
        "RELAY_TARGET_APP": child_env["RELAY_TARGET_APP"],
        "RELAY_FORCE_CAPABILITY": child_env["RELAY_FORCE_CAPABILITY"],
        "RELAY_INVOCATION_TEXT": child_env["RELAY_INVOCATION_TEXT"],
        "RELAY_REPLY_OUT": child_env["RELAY_REPLY_OUT"],
        "RELAY_WALL_OUT": child_env["RELAY_WALL_OUT"],
    }})

    print(f"[{idx:02d}] {task['id']}  {task['app']}/{task['capability']}", flush=True)
    t0 = time.monotonic()
    timed_out = False
    with (task_dir / "stdout.log").open("wb") as stdout_fh, (task_dir / "stderr.log").open("wb") as stderr_fh:
        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=stdout_fh,
            stderr=stderr_fh,
        )
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            rc = proc.wait()
    elapsed_s = round(time.monotonic() - t0, 1)

    # Per-step logs, reply.json and wall_clock.json are already routed into this
    # task's user_task/ via the env above, but run_native's agent still writes
    # traj.json + token logs into the global traj_logs/user_task/ (its hardcoded
    # _TRAJ_DIR), which the next task's _rotate_traj_dir would rotate away. Merge
    # that global dir in to honor the user_task/traj.json this script promises.
    # Best-effort: a logging gap must not drop the task result.
    global_traj = REPO_ROOT / "traj_logs" / "user_task"
    if global_traj.is_dir():
        try:
            shutil.copytree(global_traj, task_dir / "user_task", dirs_exist_ok=True)
        except OSError as e:
            print(f"[{idx:02d}] warn: failed to copy traj into {task_dir}: {e}", flush=True)

    run = {
        "id": task["id"],
        "app": task["app"],
        "capability": task["capability"],
        "category": task.get("category"),
        "difficulty": task.get("difficulty"),
        "handoff_required": task.get("handoff_required"),
        "task_dir": str(task_dir.relative_to(REPO_ROOT)),
        "returncode": rc,
        "timed_out": timed_out,
        "elapsed_s": elapsed_s,
    }
    wall = _read_json(task_dir / "user_task" / "wall_clock.json")
    if isinstance(wall, dict):
        run["task_wall_s"] = wall.get("wall_s")
    reply = _read_json(task_dir / "user_task" / "reply.json")
    if isinstance(reply, dict):
        txt = reply.get("reply") or reply.get("text") or ""
        run["reply_chars"] = len(txt) if isinstance(txt, str) else 0

    _write_json(task_dir / "run.json", run)
    print(
        f"     rc={rc} timeout={timed_out} elapsed={elapsed_s}s "
        f"task_wall={run.get('task_wall_s')} reply_chars={run.get('reply_chars')}",
        flush=True,
    )
    return run


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--only-id", action="append", default=None)
    p.add_argument("--start-after", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--task-timeout", type=float, default=900.0,
                   help="Seconds before killing one task; use 0 to disable")
    p.add_argument("--no-persist-server", action="store_true")
    p.add_argument("--dry-list", action="store_true",
                   help="List selected tasks and exit without running")
    args, extra_args = p.parse_known_args(argv)

    if not RUN_NATIVE.exists():
        raise SystemExit(f"run_native.py not found at {RUN_NATIVE}")

    doc, tasks = _load_tasks(args.tasks)
    selected = _select_tasks(
        tasks,
        set(args.only_id) if args.only_id else None,
        args.start_after,
        args.limit,
    )
    if not selected:
        raise SystemExit("No tasks selected")

    if args.dry_list:
        for idx, task in selected:
            print(f"{idx:02d} {task['id']} {task['app']}/{task['capability']} :: {task['instruction']}")
        return 0

    try:
        env = ensure_llm_env(ENV_FILE)
    except RuntimeError as e:
        raise SystemExit(str(e)) from e

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = (args.out_dir or (REPO_ROOT / "traj_logs" / f"single_app_benchmark_{ts}")).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    _write_json(out_root / "suite.json", {
        "suite": doc.get("suite"),
        "version": doc.get("version"),
        "generated": doc.get("generated"),
        "tasks_file": str(args.tasks),
        "selected_count": len(selected),
        "started_at": ts,
    })
    print(f"benchmark log root: {out_root}")

    timeout_s = None if args.task_timeout == 0 else args.task_timeout
    summary_path = out_root / "summary.jsonl"
    runs: list[dict[str, Any]] = []
    with summary_path.open("a", encoding="utf-8") as summary_fh:
        for idx, task in selected:
            run = _run_one(idx, task, env, out_root, extra_args, timeout_s)
            runs.append(run)
            summary_fh.write(json.dumps(run, ensure_ascii=False) + "\n")
            summary_fh.flush()

    ok = sum(1 for r in runs if r["returncode"] == 0 and not r["timed_out"])
    failed = len(runs) - ok
    final = {
        "total": len(runs),
        "ok": ok,
        "failed": failed,
        "out_root": str(out_root),
        "summary_jsonl": str(summary_path),
    }
    _write_json(out_root / "summary.json", final)
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
