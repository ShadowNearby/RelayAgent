#!/usr/bin/env python3
"""Route one natural-language request to a single app capability.

Reads every app manifest under `manifests/`, summarizes their functional
surface, asks the text LLM to pick the best app + capability, then dispatches
that request through `scripts/run_native.py` with the capability pinned via
environment variables.

Usage:
    scripts/run_nl.py "帮我点三杯蜜雪冰城蜜桃四季春"
    scripts/run_nl.py "帮我找一台适合学生的平板电脑，预算2000以内"

Any args after a literal `--` are forwarded to `run_native.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MANIFEST_DIR = REPO_ROOT / "manifests"
ENV_FILE = REPO_ROOT / ".env"
RUN_NATIVE = REPO_ROOT / "scripts" / "run_native.py"

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("'\"")
    return out


def _parse_fenced_json(text: str) -> dict[str, Any]:
    m = _FENCE_RE.search(text or "")
    payload = m.group(1) if m else text
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data).__name__}")
    return data


def _clean(s: Any) -> str:
    return " ".join(str(s or "").split())


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #


def build_catalog() -> dict[str, Any]:
    """Compact JSON-able view of available apps for the router LLM."""
    apps: list[dict[str, Any]] = []
    for path in sorted(MANIFEST_DIR.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            logger.warning(f"skip {path.name}: {e}")
            continue
        agent = doc.get("embedded_agent") or {}
        caps = []
        for c in agent.get("capabilities") or []:
            caps.append({
                "id": c.get("id"),
                "description": _clean(c.get("description")),
                "examples": c.get("example_prompts") or [],
                "executable": c.get("executable", True),
                "handoff_to_user_required": c.get("handoff_to_user_required", False),
            })
        apps.append({
            "app_id": doc.get("app_id"),
            "app_name": doc.get("app_name"),
            "agent_name": agent.get("name"),
            "agent_description": _clean(agent.get("description")),
            "capabilities": caps,
        })

    return {"apps": apps}


# --------------------------------------------------------------------------- #
# router LLM
# --------------------------------------------------------------------------- #


_ROUTER_SYSTEM = """You route a user's natural-language request to exactly one available mobile app capability.

Pick the closest app and capability from the catalog. Do not invent ids.

Return ONE JSON object inside a ```json``` fence with this shape:
  {"kind": "app", "app_id": "<id>", "capability_id": "<id>", "goal": "<sentence to give the in-app agent, rewritten if helpful>", "reason": "..."}

No prose outside the fence.
"""


def route(nl: str, catalog: dict[str, Any], llm: OpenAI, model: str) -> dict[str, Any]:
    user = (
        "Available app capabilities:\n"
        f"{json.dumps(catalog, ensure_ascii=False, indent=2)}\n\n"
        f"User request:\n{nl}\n\n"
        "Return the routing JSON now."
    )
    resp = llm.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _ROUTER_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        max_tokens=512,
    )
    raw = (resp.choices[0].message.content or "").strip()
    logger.debug(f"router raw reply: {raw}")
    data = _parse_fenced_json(raw)
    if data.get("kind") != "app":
        raise RuntimeError(f"router returned unsupported kind: {data!r}")
    return data


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


def dispatch_app(
    decision: dict,
    catalog: dict,
    env: dict[str, str],
    extra_args: list[str],
) -> int:
    app_id = decision.get("app_id")
    capability = decision.get("capability_id")
    goal = decision.get("goal") or ""
    match = next((a for a in catalog["apps"] if a["app_id"] == app_id), None)
    if not match:
        raise SystemExit(f"router picked unknown app_id={app_id!r}")
    if capability and not any(c["id"] == capability for c in match["capabilities"]):
        raise SystemExit(
            f"router picked unknown capability_id={capability!r} for {app_id!r}"
        )
    if not goal:
        raise SystemExit("router did not produce a goal for the app step")

    logger.info(
        f"dispatch app -> {app_id}  capability={capability!r}  "
        f"goal={goal!r}  (reason: {decision.get('reason')})"
    )

    child_env = {
        **env,
        **os.environ,
        "RELAY_TARGET_APP": app_id,
        "RELAY_SKIP_OPEN_APP": "1",
        "RELAY_AGENT_LAUNCH": "1",
    }
    if capability:
        child_env["RELAY_FORCE_CAPABILITY"] = capability
        child_env["RELAY_INVOCATION_TEXT"] = goal

    cmd = [
        sys.executable,
        str(RUN_NATIVE),
        app_id,
        goal,
        *extra_args,
    ]
    return subprocess.call(cmd, cwd=REPO_ROOT, env=child_env)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("nl", help="The natural-language request")
    p.add_argument("--dry-run", action="store_true",
                   help="Show the router decision but don't dispatch")
    p.add_argument("--record", nargs="?", const="", default=None, metavar="DIR",
                   help="Record device screen via adb screenrecord. "
                        "Optional DIR overrides the default traj_logs/recordings/<ts>/.")
    args, extra = p.parse_known_args(argv)

    env = _load_dotenv(ENV_FILE)
    for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        v = os.environ.get(k) or env.get(k)
        if not v:
            sys.exit(f"Missing required config: {k} (set in .env or shell env)")
        env[k] = v

    catalog = build_catalog()
    logger.info(f"catalog: {len(catalog['apps'])} apps")
    llm = OpenAI(base_url=env["LLM_BASE_URL"], api_key=env["LLM_API_KEY"])
    decision = route(args.nl, catalog, llm, env["LLM_MODEL"])
    print(json.dumps(decision, ensure_ascii=False, indent=2))

    if args.dry_run:
        return 0

    if args.record is not None:
        from datetime import datetime
        out_dir = (
            Path(args.record).expanduser().resolve()
            if args.record
            else REPO_ROOT / "traj_logs" / "recordings" / datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        os.environ.setdefault("RELAY_SKIP_STEP_SCREENSHOT", "1")
        os.environ["RELAY_RECORD_DIR"] = str(out_dir)
        logger.info("recording mode -> RELAY_SKIP_STEP_SCREENSHOT=1")
        logger.info(f"screen recording (agent-owned, framework-excluded) -> {out_dir}")

    return dispatch_app(decision, catalog, env, extra)


if __name__ == "__main__":
    sys.exit(main())
