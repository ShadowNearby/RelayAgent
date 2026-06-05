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
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mw_server import ensure_server  # noqa: E402

ENV_FILE = REPO_ROOT / ".env"
# Default agent; override with RELAY_AGENT_FILE (e.g. the a11y-text baseline,
# agents/a11y_agent.py, used for the §8.9 input-modality ablation).
AGENT_FILE = Path(os.environ["RELAY_AGENT_FILE"]).resolve() if os.getenv(
    "RELAY_AGENT_FILE"
) else REPO_ROOT / "agents" / "relay_agent.py"
MW_BIN = REPO_ROOT / ".venv" / "bin" / "mw"


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

    # The app cold-launch is DEFERRED to the agent's first predict (set
    # RELAY_AGENT_LAUNCH=1 below). By then MobileWorld's framework cold-start
    # (~2.7s) is done, so it lands before the launch and is excluded from the
    # task wall-clock the agent writes. The planner still skips its own open_app
    # step (RELAY_SKIP_OPEN_APP=1) because the agent owns the launch.

    # The adapter reads RELAY_TARGET_APP at construction time.
    # Priority: explicit overrides (RELAY_*) > shell env > .env file.
    # `env_vars` from .env is the lowest layer so a user can override any
    # LLM_* / RELAY_* setting from their shell without editing .env.
    child_env = {
        **env_vars,
        **os.environ,
        "RELAY_TARGET_APP": args.app,
        "RELAY_SKIP_OPEN_APP": "1",
        # Agent owns the cold-launch (deferred to first predict, post-framework).
        "RELAY_AGENT_LAUNCH": "1",
        # MobileWorld's server sleeps on every no-op WAIT action (default 1.0s).
        # Our wait_for_reply polls with WAIT and runs its own stability
        # detection, so the full second just inflates each poll tick. Trim it.
        # Honoured only by the patched MW fork (MW_WAIT_SECONDS); harmless on
        # upstream. Caller can override by exporting MW_WAIT_SECONDS.
        # NB: this is a SERVER-side knob — it only takes effect for a server we
        # start. ensure_server() bakes it into the persistent server at spawn;
        # for a reused server it's a no-op (restart to rebake).
        "MW_WAIT_SECONDS": os.getenv("MW_WAIT_SECONDS", "0.2"),
    }

    # Reuse a persistent server instead of letting `mw test` start (and tear
    # down) a fresh one every run. ensure_server returns the --aw_host URL;
    # None means "couldn't, let mw test self-start" (or opted out via
    # RELAY_NO_PERSIST_SERVER=1). Skip if the caller already passed --aw_host.
    if not any(a == "--aw_host" or a == "--aw-host" or a.startswith("--aw_host=")
               or a.startswith("--aw-host=") for a in extra):
        aw_host = ensure_server(child_env)
        if aw_host:
            extra = ["--aw_host", aw_host, *extra]

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
    # wall_clock.json (the framework-EXCLUDED task wall-clock that
    # aggregate_metrics.py reads) is now written by the agent at process exit —
    # it anchors at the agent's first predict, after MobileWorld's framework
    # cold-start. Here we only print the GROSS subprocess time (incl. framework)
    # to stderr for reference; we no longer write the file. Gated by RELAY_TIMING.
    timing = os.getenv("RELAY_TIMING", "0") == "1"
    t0 = time.monotonic()
    rc = subprocess.call(cmd, cwd=REPO_ROOT, env=child_env)
    if timing:
        gross_s = round(time.monotonic() - t0, 1)
        print(f"▶ gross wall_s={gross_s} (incl. framework; task wall_s in "
              f"traj_logs/user_task/wall_clock.json excludes it)", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
