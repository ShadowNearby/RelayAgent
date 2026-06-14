"""Leg executors — how one app leg's native run is invoked.

The host default spawns a fresh `python -m agents.runtime.native_runner`
subprocess per leg (crash isolation, per-leg env). The Android / no-subprocess
mode runs the leg in-process via `native_runner.main`. Split out of
`flow_runner.py`; `FlowRunner` picks one via `_default_leg_executor`.
"""

from __future__ import annotations

import os
import subprocess
import sys

from loguru import logger

from agents.flow.flow_runner_util import NATIVE_RUNNER_MODULE, REPO_ROOT


class SubprocessLegExecutor:
    """Host default: one fresh `python -m agents.runtime.native_runner` per leg
    (crash isolation; per-leg env via the child process env). Byte-identical
    to the pre-seam inline subprocess.call.

    stdin is fed empty so the final ask_user handoff (when present) closes
    cleanly with EOF rather than blocking the flow."""

    def run(self, app: str, prompt: str, child_env: dict[str, str],
            extra_args: list[str]) -> int:
        cmd = [sys.executable, "-m", NATIVE_RUNNER_MODULE, app, prompt, *extra_args]
        return subprocess.call(cmd, cwd=REPO_ROOT, env=child_env, stdin=subprocess.DEVNULL)


class InProcessLegExecutor:
    """Android / no-subprocess mode: swap os.environ to the leg env, run the
    leg in this process via native_runner.main (same argparse path as the
    CLI), restore env in a finally. Legs are sequential, so a plain swap is
    safe; the agent module is re-executed per leg by _load_agent_class, so
    module-level env reads re-resolve.

    Unlike the subprocess executor, stdin is NOT detached — the in-task
    ask_user handoff goes through the InteractionProvider instead of EOF."""

    def run(self, app: str, prompt: str, child_env: dict[str, str],
            extra_args: list[str]) -> int:
        from agents.runtime import native_runner

        snapshot = dict(os.environ)
        os.environ.clear()
        os.environ.update(child_env)
        try:
            return int(native_runner.main([app, prompt, *extra_args]) or 0)
        except SystemExit as e:  # native_runner maps config errors to sys.exit
            logger.warning(f"in-process leg exited: {e.code}")
            return e.code if isinstance(e.code, int) else 1
        except Exception:
            logger.exception(f"in-process leg crashed for app={app}")
            return 1
        finally:
            os.environ.clear()
            os.environ.update(snapshot)


def _default_leg_executor():
    if os.getenv("RELAY_LEG_EXECUTOR", "subprocess") == "inprocess":
        return InProcessLegExecutor()
    return SubprocessLegExecutor()
