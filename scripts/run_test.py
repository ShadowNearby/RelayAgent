#!/usr/bin/env python3
"""Run RelayAgent against a goal via MobileWorld's `mw test`.

Loads LLM_* config from the repo-root `.env`, points --agent-type at
`agents/relay_agent.py`, and forwards any extra args straight to
`mw test` so flags like --max-step are pass-through.

Usage:
    scripts/run_test.py com.aliyun.tongyi "帮我点三杯蜜雪冰城蜜桃四季春"
    scripts/run_test.py com.autonavi.minimap "帮我导航回家" --max-step 40
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents._adb import cold_launch as _cold_launch  # noqa: E402

ENV_FILE = REPO_ROOT / ".env"
AGENT_FILE = REPO_ROOT / "agents" / "relay_agent.py"
MW_BIN = REPO_ROOT / ".venv" / "bin" / "mw"


def cold_launch(package: str, settle_seconds: float = 2.5) -> None:
    """Cold-launch the target app via the shared helper. Mandatory before
    any test run — see agents/_adb.py for the policy rationale."""
    print(f"▶ cold-launching {package} (force-stop + monkey LAUNCHER) ...",
          file=sys.stderr)
    try:
        _cold_launch(package, settle_seconds=settle_seconds)
    except RuntimeError as e:
        sys.exit(f"{e}\nCheck `adb devices` and the package id.")


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser; no quoting / interpolation tricks."""
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


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "app", help="Target app package id (e.g. com.aliyun.tongyi)"
    )
    p.add_argument("goal", help="Natural-language task for the agent")
    p.add_argument("--model", help="Override LLM_MODEL from .env")
    p.add_argument("--base-url", help="Override LLM_BASE_URL from .env")
    p.add_argument("--api-key", help="Override LLM_API_KEY from .env")
    args, extra = p.parse_known_args()

    if not MW_BIN.exists():
        sys.exit(
            f"mw binary not found at {MW_BIN}. "
            "Install MobileWorld into .venv first (see CLAUDE.md)."
        )
    if not AGENT_FILE.exists():
        sys.exit(f"agent file missing: {AGENT_FILE}")

    env_vars = load_dotenv(ENV_FILE)
    base_url = args.base_url or os.getenv("LLM_BASE_URL") or env_vars.get("LLM_BASE_URL")
    api_key = args.api_key or os.getenv("LLM_API_KEY") or env_vars.get("LLM_API_KEY")
    model = args.model or os.getenv("LLM_MODEL") or env_vars.get("LLM_MODEL")
    missing = [n for n, v in [("LLM_BASE_URL", base_url), ("LLM_API_KEY", api_key), ("LLM_MODEL", model)] if not v]
    if missing:
        sys.exit(f"Missing required config: {', '.join(missing)}. Set in .env or pass via flags.")

    # Pre-launch the app so MobileWorld's first observation is already the
    # app's home screen. The planner then skips its own open_app step.
    cold_launch(args.app)

    # The adapter reads RELAY_TARGET_APP at construction time.
    # Priority: explicit overrides (RELAY_*) > shell env > .env file.
    # `env_vars` from .env is the lowest layer so a user can override any
    # LLM_* / RELAY_* setting from their shell without editing .env.
    child_env = {
        **env_vars,
        **os.environ,
        "RELAY_TARGET_APP": args.app,
        "RELAY_SKIP_OPEN_APP": "1",
        # MobileWorld's server sleeps on every no-op WAIT action (default 1.0s).
        # Our wait_for_reply polls with WAIT and runs its own stability
        # detection, so the full second just inflates each poll tick. Trim it.
        # Honoured only by the patched MW fork (MW_WAIT_SECONDS); harmless on
        # upstream. Caller can override by exporting MW_WAIT_SECONDS.
        "MW_WAIT_SECONDS": os.getenv("MW_WAIT_SECONDS", "0.2"),
    }

    cmd = [
        str(MW_BIN), "test", args.goal,
        "--agent-type", str(AGENT_FILE),
        "--model_name", model,
        "--llm_base_url", base_url,
        "--api_key", api_key,
    ]
    # MobileWorld sleeps `step_wait_time` (default 1.0s) before *every* step's
    # screenshot to let UI animations settle. Our grounding goes through
    # uiautomator (its own retries) and wait_for_reply has its own stability
    # detection, so the full 1.0s settle is overkill — trim to 0.2s, shaving
    # ~0.8s off every step across the whole run. Overridable: only injected
    # when the caller didn't pass their own --step_wait_time.
    if not any(a == "--step_wait_time" or a.startswith("--step_wait_time=") for a in extra):
        cmd += ["--step_wait_time", os.getenv("RELAY_STEP_WAIT", "0.2")]
    cmd += [*extra]
    print(
        f"▶ RELAY_TARGET_APP={args.app}  goal={args.goal!r}  "
        f"model={model}  (base_url + key from .env / flags, key redacted)",
        file=sys.stderr,
    )
    # Optional wall-clock timing. Gated by RELAY_TIMING=1 so normal runs pay
    # nothing; when on, we time just the `mw test` call (not cold-launch, to
    # match the manual-run measurements) and drop a wall_clock.json into the
    # live traj dir so it travels with the traj when the caller mv's it.
    # aggregate_metrics.py reads this file for the wall_s column.
    timing = os.getenv("RELAY_TIMING", "0") == "1"
    t0 = time.monotonic()
    rc = subprocess.call(cmd, cwd=REPO_ROOT, env=child_env)
    if timing:
        wall_s = round(time.monotonic() - t0, 1)
        traj_dir = REPO_ROOT / "traj_logs" / "user_task"
        try:
            if traj_dir.is_dir():
                (traj_dir / "wall_clock.json").write_text(
                    json.dumps({"wall_s": wall_s, "phase": "mw_test"}),
                    encoding="utf-8",
                )
        except OSError as e:
            print(f"▶ timing write failed: {e}", file=sys.stderr)
        print(f"▶ wall_s={wall_s}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
