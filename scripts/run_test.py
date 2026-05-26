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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents._adb import cold_launch as _cold_launch  # noqa: E402

ENV_FILE = REPO_ROOT / ".env"
AGENT_FILE = REPO_ROOT / "agents" / "appcards_agent.py"
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

    # The adapter reads APPCARDS_TARGET_APP at construction time.
    # Priority: explicit overrides (APPCARDS_*) > shell env > .env file.
    # `env_vars` from .env is the lowest layer so a user can override any
    # LLM_* / APPCARDS_* setting from their shell without editing .env.
    child_env = {
        **env_vars,
        **os.environ,
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
