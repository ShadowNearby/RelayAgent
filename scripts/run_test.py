#!/usr/bin/env python3
"""Run AppCardsAgent against a goal via MobileWorld's `mw test`.

Loads LLM_* config from the repo-root `.env`, points --agent-type at
`agents/appcards_agent.py`, and forwards any extra args straight to
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
ENV_FILE = REPO_ROOT / ".env"
AGENT_FILE = REPO_ROOT / "agents" / "appcards_agent.py"
MW_BIN = REPO_ROOT / ".venv" / "bin" / "mw"


def cold_launch(package: str, settle_seconds: float = 2.5) -> None:
    """Cold-launch policy — MANDATORY before any test run.

    Always force-stop the target app FIRST, then (re-)launch via the
    standard launcher intent. This guarantees MobileWorld's first
    observation is the app's clean home surface — not a stale modal,
    half-finished chat thread, expired session sheet, or whatever the
    previous run left behind.

    The agent (`appcards_agent._materialize`) mirrors this policy
    independently when it handles the `open_app` step, so direct
    `mw test` invocations that bypass this script still cold-start
    cleanly. Both call sites must stay in sync — DO NOT remove
    either one.
    """
    print(f"▶ cold-launching {package} (force-stop + monkey LAUNCHER) ...",
          file=sys.stderr)
    fs = subprocess.run(
        ["adb", "shell", "am", "force-stop", package],
        check=False, capture_output=True, text=True, timeout=10,
    )
    if fs.returncode != 0:
        # force-stop is best-effort; report and continue to launch.
        print(f"  ! force-stop returned {fs.returncode}: "
              f"{(fs.stderr or fs.stdout).strip()}", file=sys.stderr)
    res = subprocess.run(
        ["adb", "shell", "monkey", "-p", package,
         "-c", "android.intent.category.LAUNCHER", "1"],
        check=False, capture_output=True, text=True, timeout=10,
    )
    if res.returncode != 0 or "No activities found" in (res.stdout + res.stderr):
        sys.exit(
            f"Failed to launch {package} via adb monkey.\n"
            f"stdout: {res.stdout.strip()}\nstderr: {res.stderr.strip()}\n"
            "Check `adb devices` and the package id."
        )
    time.sleep(settle_seconds)


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

    # The adapter reads APPCARDS_TARGET_APP at construction time.
    child_env = {
        **os.environ,
        **env_vars,
        "APPCARDS_TARGET_APP": args.app,
        "APPCARDS_SKIP_OPEN_APP": "1",
    }

    cmd = [
        str(MW_BIN), "test", args.goal,
        "--agent-type", str(AGENT_FILE),
        "--model_name", model,
        "--llm_base_url", base_url,
        "--api_key", api_key,
        *extra,
    ]
    print(
        f"▶ APPCARDS_TARGET_APP={args.app}  goal={args.goal!r}  "
        f"model={model}  (base_url + key from .env / flags, key redacted)",
        file=sys.stderr,
    )
    return subprocess.call(cmd, cwd=REPO_ROOT, env=child_env)


if __name__ == "__main__":
    sys.exit(main())
