#!/usr/bin/env python3
"""Run RelayAgent against a goal — direct-adb native runtime, no server.

The single-app entry point. Loads agents/relay_agent.py, and the actions
predict() returns are executed by direct `adb` calls
(agents/native_runtime.py) in an in-process obs→predict→execute loop.

Usage:
    scripts/run_native.py com.aliyun.tongyi "帮我点三杯蜜雪冰城蜜桃四季春"
    scripts/run_native.py com.autonavi.minimap "帮我导航回家" --max-step 40

The agent writes the task wall-clock to traj_logs/user_task/wall_clock.json,
anchored at its first predict.
"""
from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ENV_FILE = REPO_ROOT / ".env"
AGENT_FILE = Path(os.environ["RELAY_AGENT_FILE"]).resolve() if os.getenv(
    "RELAY_AGENT_FILE"
) else REPO_ROOT / "agents" / "relay_agent.py"
TRAJ_DIR = REPO_ROOT / "traj_logs" / "user_task"


def load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("'\"")
    return out


def _rotate_traj_dir() -> None:
    """Startup rotation: move a prior user_task/ to a timestamped backup so
    this run's output is always in traj_logs/user_task/ (see CLAUDE.md
    'Trajectory 日志目录'). Then seed an empty traj.json so the agent's
    _append_llm_call has a file to append to."""
    if TRAJ_DIR.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = TRAJ_DIR.parent / f"user_task_backup_{ts}"
        try:
            TRAJ_DIR.rename(backup)
        except OSError:
            pass
    TRAJ_DIR.mkdir(parents=True, exist_ok=True)
    (TRAJ_DIR / "traj.json").write_text("{}", encoding="utf-8")


def _load_agent_class(path: Path):
    """Minimal file→agent-class loader. Picks the alphabetically-first
    BaseAgent subclass — the deliberate alias `_MCPAgentBase` sorts after
    `RelayAgent` so RelayAgent wins (see relay_agent.py header)."""
    from agents.agent_base import BaseAgent

    spec = importlib.util.spec_from_file_location(path.stem, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    classes = [
        (n, o)
        for n, o in inspect.getmembers(module, inspect.isclass)
        if issubclass(o, BaseAgent) and o is not BaseAgent
    ]
    if not classes:
        sys.exit(f"No BaseAgent subclass found in {path}")
    classes.sort(key=lambda t: t[0])
    return classes[0][1]


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("app", help="Target app package id (e.g. com.aliyun.tongyi)")
    p.add_argument("goal", help="Natural-language task for the agent")
    p.add_argument("--model", help="Override LLM_MODEL from .env")
    p.add_argument("--base-url", help="Override LLM_BASE_URL from .env")
    p.add_argument("--api-key", help="Override LLM_API_KEY from .env")
    p.add_argument("--max-step", type=int, default=-1, help="Max steps (-1 = unlimited)")
    p.add_argument("--step_wait_time", type=float, default=None,
                   help="Per-step settle before screenshot (s); else RELAY_STEP_WAIT/0.2")
    p.add_argument("--keep-ime", action="store_true",
                   help="Do not restore the device IME at exit (leave AdbKeyboard active)")
    args, unknown = p.parse_known_args()
    if unknown:
        print(f"▶ [native] ignoring unrecognized args: {unknown}", file=sys.stderr)

    if not AGENT_FILE.exists():
        sys.exit(f"agent file missing: {AGENT_FILE}")

    env_vars = load_dotenv(ENV_FILE)
    base_url = args.base_url or os.getenv("LLM_BASE_URL") or env_vars.get("LLM_BASE_URL")
    api_key = args.api_key or os.getenv("LLM_API_KEY") or env_vars.get("LLM_API_KEY")
    model = args.model or os.getenv("LLM_MODEL") or env_vars.get("LLM_MODEL")
    missing = [n for n, v in [("LLM_BASE_URL", base_url), ("LLM_API_KEY", api_key),
                              ("LLM_MODEL", model)] if not v]
    if missing:
        sys.exit(f"Missing required config: {', '.join(missing)}. Set in .env or via flags.")

    # Populate env BEFORE the agent module is loaded/constructed: the agent
    # owns the deferred cold-launch and the planner skips its own open_app.
    # .env is the lowest layer; shell env wins.
    for k, v in env_vars.items():
        os.environ.setdefault(k, v)
    os.environ["RELAY_TARGET_APP"] = args.app
    os.environ["RELAY_SKIP_OPEN_APP"] = "1"
    os.environ["RELAY_AGENT_LAUNCH"] = "1"
    os.environ.setdefault("RELAY_WAIT_SECONDS", "0.2")

    _rotate_traj_dir()

    # Import the native substrate AFTER sys.path/env are set.
    from agents.native_runtime import NativeEnv, activate_adb_keyboard, reset_ime, run_task

    step_wait = (
        args.step_wait_time
        if args.step_wait_time is not None
        else float(os.getenv("RELAY_STEP_WAIT", "0.2"))
    )
    env = NativeEnv(step_wait_time=step_wait)

    agent_cls = _load_agent_class(AGENT_FILE)
    agent = agent_cls(model_name=model, llm_base_url=base_url, api_key=api_key, env=env)

    print(
        f"▶ [native] RELAY_TARGET_APP={args.app}  goal={args.goal!r}  model={model}  "
        f"(direct adb)",
        file=sys.stderr,
    )

    if not activate_adb_keyboard():
        print("⚠️  AdbKeyboard not active — input_text steps may fail.", file=sys.stderr)

    t0 = time.monotonic()
    summary = {}
    try:
        summary = run_task(args.goal, agent, env, max_step=args.max_step)
    finally:
        # Flush the agent's framework-excluded wall-clock now (idempotent; the
        # atexit copy will no-op). Then read it back for the summary.
        finalize = getattr(agent, "_finalize_task", None)
        if callable(finalize):
            finalize()
        if not args.keep_ime:
            reset_ime()

    gross_s = round(time.monotonic() - t0, 1)
    wall_s = None
    wall_path = TRAJ_DIR / "wall_clock.json"
    if wall_path.exists():
        try:
            wall_s = json.loads(wall_path.read_text()).get("wall_s")
        except (OSError, json.JSONDecodeError):
            pass
    usage = summary.get("token_usage", {})
    print(
        f"▶ [native] done  steps={summary.get('steps')}  "
        f"task_wall_s={wall_s}  gross_s={gross_s}  "
        f"tokens(prompt/completion/total)="
        f"{usage.get('prompt_tokens')}/{usage.get('completion_tokens')}/{usage.get('total_tokens')}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
