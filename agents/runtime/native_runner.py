"""Single-app native runner for RelayAgent.

Loads a BaseAgent implementation and drives it through the direct-adb native
runtime in an in-process obs -> predict -> execute loop. This module is also
the subprocess target used by the flow and benchmark drivers:

    python -m agents.runtime.native_runner com.aliyun.tongyi "order three drinks"
    python -m agents.runtime.native_runner com.autonavi.minimap "navigate home" --max-step 40

The agent writes the task wall-clock to traj_logs/user_task/wall_clock.json,
anchored at its first predict.

`run_leg` is the importable equivalent of one CLI invocation — the Android
build (no subprocess spawning under Chaquopy) and flow_runner's
InProcessLegExecutor call it directly. All run state (trajectory dir, target
app, LLM config) is read from env **per call**, never at import, so one
process can run many legs with different env.
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
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ENV_FILE = REPO_ROOT / ".env"
DEFAULT_AGENT_FILE = REPO_ROOT / "agents" / "agent" / "relay_agent.py"
SUMMARY_OUT_ENV = "RELAY_SUMMARY_OUT"

from agents.runtime.runtime_config import resolve_llm_config  # noqa: E402


def _agent_file() -> Path:
    override = os.getenv("RELAY_AGENT_FILE")
    return Path(override).resolve() if override else DEFAULT_AGENT_FILE


def _resolve_traj_dir() -> tuple[Path, bool]:
    """This run's trajectory dir (traj.json + steps/ + agent_reply.json) and
    whether it was pinned via RELAY_TRAJ_DIR. Defaults to the shared global
    dir; the flow runner pins it per leg so each leg writes straight into its
    own dir. Resolved per call (NOT at import) so in-process legs honor
    per-leg env."""
    env = os.getenv("RELAY_TRAJ_DIR")
    if env:
        return Path(env), True
    return REPO_ROOT / "traj_logs" / "user_task", False


def _rotate_traj_dir() -> Path:
    """Move a prior user_task/ aside so this run owns traj_logs/user_task/.

    When RELAY_TRAJ_DIR pins a per-run dir (e.g. a flow leg), there's no shared
    dir to reclaim — each leg dir is already unique — so skip the backup rename
    and just ensure the dir exists and seed an empty traj.json."""
    traj_dir, pinned = _resolve_traj_dir()
    if not pinned and traj_dir.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = traj_dir.parent / f"user_task_backup_{ts}"
        try:
            traj_dir.rename(backup)
        except OSError:
            pass
    traj_dir.mkdir(parents=True, exist_ok=True)
    (traj_dir / "traj.json").write_text("{}", encoding="utf-8")
    return traj_dir


def _agent_spec(path: Path):
    """Module spec for the agent at `path`. Prefers the on-disk file (host
    checkout layout). In packaged runtimes (Chaquopy AssetFinder zip) the
    agents/*.py sources are importable modules but never exist as files —
    fall back to the package spec so on-device runs load the same agent."""
    if path.exists():
        return importlib.util.spec_from_file_location(path.stem, str(path))
    try:
        # Agent modules live under the agents.agent subpackage (relay_agent,
        # a11y_agent); the on-disk default is agents/agent/relay_agent.py.
        return importlib.util.find_spec(f"agents.agent.{path.stem}")
    except (ImportError, ValueError):
        return None


def _load_agent_class(path: Path):
    """Load the alphabetically first BaseAgent subclass from a Python file.

    Re-executes the module on every call (one call per leg): the agent module
    reads RELAY_* env at module level, and per-leg env must re-resolve."""
    from agents.agent.agent_base import BaseAgent

    spec = _agent_spec(path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load agent file: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    classes = [
        (name, obj)
        for name, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, BaseAgent) and obj is not BaseAgent
    ]
    if not classes:
        raise RuntimeError(f"No BaseAgent subclass found in {path}")
    classes.sort(key=lambda item: item[0])
    return classes[0][1]


def run_leg(
    app: str,
    goal: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    max_step: int = -1,
    step_wait_time: float | None = None,
    keep_ime: bool = False,
) -> dict:
    """Run one single-app leg in this process. Returns the run summary.

    Raises RuntimeError on config/agent-load failure (the CLI maps that to
    sys.exit; in-process callers handle it). Mutates os.environ for the
    duration of the run — in-process multi-leg callers snapshot/restore
    around it (see flow_runner.InProcessLegExecutor)."""
    agent_file = _agent_file()
    if _agent_spec(agent_file) is None:
        raise RuntimeError(f"agent file missing: {agent_file}")

    env_vars, base_url, api_key, model = resolve_llm_config(
        ENV_FILE,
        model=model,
        base_url=base_url,
        api_key=api_key,
    )

    # Populate env before loading the agent module. The agent owns deferred
    # cold-launch, and the planner skips its own open_app step.
    for key, value in env_vars.items():
        os.environ.setdefault(key, value)
    os.environ["RELAY_TARGET_APP"] = app
    os.environ["RELAY_SKIP_OPEN_APP"] = "1"
    os.environ["RELAY_AGENT_LAUNCH"] = "1"
    os.environ.setdefault("RELAY_WAIT_SECONDS", "0.2")

    traj_dir = _rotate_traj_dir()

    from agents.device import get_backend
    from agents.runtime.native_runtime import NativeEnv, run_task

    backend = get_backend()
    step_wait = (
        step_wait_time
        if step_wait_time is not None
        else float(os.getenv("RELAY_STEP_WAIT", "0.5"))
    )
    env = NativeEnv(step_wait_time=step_wait, backend=backend)

    agent_cls = _load_agent_class(agent_file)
    agent = agent_cls(model_name=model, llm_base_url=base_url, api_key=api_key, env=env)

    print(
        f"[native] RELAY_TARGET_APP={app} goal={goal!r} model={model} "
        f"(direct adb)",
        file=sys.stderr,
    )

    try:
        input_channel_ok = backend.setup_input_channel()
    except NotImplementedError as exc:
        # ios / harmonyos skeleton backends: surface the one-line pointer
        # instead of a traceback from deep inside the run loop.
        raise RuntimeError(f"[native] {type(backend).__name__}: {exc}") from exc
    if not input_channel_ok:
        # Without AdbKeyboard there is no adb path for CJK input — a goal that
        # needs it WILL fail at the typing step, so fail fast here instead of
        # paying a doomed device run. ASCII goals can limp through `input text`.
        if any(ord(ch) > 127 for ch in goal):
            raise RuntimeError(
                "[native] input channel unavailable (AdbKeyboard missing?) and "
                "the goal contains non-ASCII text — the typed invocation would "
                "fail mid-run. Install ADBKeyboard.apk (env_fail)."
            )
        print(
            "Input channel not active; falling back to ASCII `input text` (degraded).",
            file=sys.stderr,
        )

    start = time.monotonic()
    summary: dict = {}
    try:
        summary = run_task(goal, agent, env, max_step=max_step)
    finally:
        finalize = getattr(agent, "_finalize_task", None)
        if callable(finalize):
            finalize()
        if not keep_ime:
            backend.teardown_input_channel()
        # Always stamp the agent's accumulated token total onto the summary —
        # even when run_task raised mid-leg, the agent has been counting usage,
        # and a bare {} summary would silently drop it from the run_plan token
        # accounting. On normal completion run_task already filled it; this only
        # backfills the crash path. Best-effort: never mask the original error.
        if not summary.get("token_usage"):
            try:
                get_usage = getattr(agent, "get_total_token_usage", None)
                if callable(get_usage):
                    summary["token_usage"] = get_usage()
            except Exception as exc:  # noqa: BLE001
                print(f"[native] failed to read token usage: {exc}", file=sys.stderr)
        summary_out = os.getenv(SUMMARY_OUT_ENV)
        if summary_out:
            try:
                out_path = Path(summary_out)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                from agents.flow.user_profile import redact_obj

                out_path.write_text(
                    json.dumps(redact_obj(summary), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError as exc:
                print(f"[native] failed to write summary: {exc}", file=sys.stderr)

    gross_s = round(time.monotonic() - start, 1)
    wall_s = None
    wall_path = traj_dir / "wall_clock.json"
    if wall_path.exists():
        try:
            wall_s = json.loads(wall_path.read_text()).get("wall_s")
        except (OSError, json.JSONDecodeError):
            pass
    usage = summary.get("token_usage", {})
    print(
        f"[native] done steps={summary.get('steps')} "
        f"task_wall_s={wall_s} gross_s={gross_s} "
        f"tokens(prompt/completion/total)="
        f"{usage.get('prompt_tokens')}/{usage.get('completion_tokens')}/"
        f"{usage.get('total_tokens')}",
        file=sys.stderr,
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("app", help="Target app package id, e.g. com.aliyun.tongyi")
    parser.add_argument("goal", help="Natural-language task for the agent")
    parser.add_argument("--model", help="Override LLM_MODEL from .env")
    parser.add_argument("--base-url", help="Override LLM_BASE_URL from .env")
    parser.add_argument("--api-key", help="Override LLM_API_KEY from .env")
    parser.add_argument("--max-step", type=int, default=-1, help="Max steps (-1 = unlimited)")
    parser.add_argument(
        "--step_wait_time",
        type=float,
        default=None,
        help="Per-step settle before screenshot (s); else RELAY_STEP_WAIT/0.5",
    )
    parser.add_argument(
        "--keep-ime",
        action="store_true",
        help="Do not restore the device IME at exit; leave AdbKeyboard active",
    )
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        print(f"[native] ignoring unrecognized args: {unknown}", file=sys.stderr)

    try:
        run_leg(
            args.app,
            args.goal,
            model=args.model,
            base_url=args.base_url,
            api_key=args.api_key,
            max_step=args.max_step,
            step_wait_time=args.step_wait_time,
            keep_ime=args.keep_ime,
        )
    except RuntimeError as exc:
        sys.exit(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
