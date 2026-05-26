#!/usr/bin/env python3
"""Run a task from a single natural-language sentence.

Reads every app manifest under `manifests/` and every flow YAML under
`manifests/_flows/`, summarizes their functional surface, and asks the
text LLM to pick the best match for the user's sentence:

  - a flow (multi-app cowork) — dispatched via FlowRunner with --nl, OR
  - a single app + capability — dispatched via `mw test` with the
    capability pinned through APPCARDS_FORCE_CAPABILITY.

Usage:
    scripts/run_nl.py "帮我点三杯蜜雪冰城蜜桃四季春"
    scripts/run_nl.py "在上海找三家评价好的小众书店，挑一家打车过去"

Any args after a literal `--` are forwarded to the underlying runner
(FlowRunner forwards them to each `mw test`; the single-app path
forwards them to `mw test` directly).
"""
from __future__ import annotations

import argparse
import json
import os
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

from agents import _recorder  # noqa: E402
from agents._adb import cold_launch as _cold_launch  # noqa: E402
from agents.flow_runner import (  # noqa: E402
    FlowRunner,
    _load_dotenv,
    _parse_fenced_json,
)

MANIFEST_DIR = REPO_ROOT / "manifests"
FLOW_DIR = MANIFEST_DIR / "_flows"
ENV_FILE = REPO_ROOT / ".env"
AGENT_FILE = REPO_ROOT / "agents" / "appcards_agent.py"
MW_BIN = REPO_ROOT / ".venv" / "bin" / "mw"


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #


def _clean(s: Any) -> str:
    return " ".join(str(s or "").split())


def build_catalog() -> dict[str, Any]:
    """Compact JSON-able view of available apps and flows for the router LLM."""
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

    flows: list[dict[str, Any]] = []
    for path in sorted(FLOW_DIR.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            logger.warning(f"skip {path.name}: {e}")
            continue
        inputs = {}
        for name, spec in (doc.get("inputs") or {}).items():
            inputs[name] = {
                "type": spec.get("type", "string"),
                "description": _clean(spec.get("description")),
                "default": spec.get("default"),
            }
        flows.append({
            "flow_id": doc.get("flow_id"),
            "path": str(path.relative_to(REPO_ROOT)),
            "description": _clean(doc.get("description")),
            "apps_required": [a.get("app_id") for a in (doc.get("apps_required") or [])],
            "inputs": inputs,
        })

    return {"apps": apps, "flows": flows}


# --------------------------------------------------------------------------- #
# router LLM
# --------------------------------------------------------------------------- #


_ROUTER_SYSTEM = (
    "You route a user's natural-language request to ONE of the available "
    "executors. Two kinds exist:\n"
    "  - flow: a multi-app cowork pipeline. Pick this when the request "
    "spans two apps, or when discovery + action belong to different apps.\n"
    "  - app:  a single app+capability invocation. Pick this for any "
    "in-app task that a single embedded agent can complete.\n\n"
    "Return ONE JSON object inside a ```json``` fence with this shape:\n"
    "  {\"kind\": \"flow\", \"flow_id\": \"<id>\", \"reason\": \"...\"}\n"
    "  {\"kind\": \"app\", \"app_id\": \"<id>\", \"capability_id\": \"<id>\", "
    "\"goal\": \"<sentence to give the in-app agent, rewritten if helpful>\", "
    "\"reason\": \"...\"}\n"
    "No prose outside the fence. Pick the closest match; do NOT invent ids."
)


def route(nl: str, catalog: dict[str, Any], llm: OpenAI, model: str) -> dict[str, Any]:
    user = (
        "Available executors:\n"
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
    if not isinstance(data, dict) or "kind" not in data:
        raise RuntimeError(f"router returned malformed JSON: {raw}")
    return data


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


def dispatch_flow(decision: dict, nl: str, catalog: dict, extra_mw_args: list[str]) -> int:
    flow_id = decision.get("flow_id")
    match = next((f for f in catalog["flows"] if f["flow_id"] == flow_id), None)
    if not match:
        raise SystemExit(f"router picked unknown flow_id={flow_id!r}")
    flow_path = (REPO_ROOT / match["path"]).resolve()
    logger.info(f"dispatch flow → {flow_path.name}  (reason: {decision.get('reason')})")
    runner = FlowRunner(
        flow_path=flow_path,
        nl_request=nl,
        extra_mw_args=extra_mw_args,
    )
    runner.run()
    return 0


def dispatch_app(
    decision: dict,
    catalog: dict,
    env: dict[str, str],
    extra_mw_args: list[str],
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
        f"dispatch app → {app_id}  capability={capability!r}  "
        f"goal={goal!r}  (reason: {decision.get('reason')})"
    )

    _cold_launch(app_id)
    child_env = {
        **env,
        **os.environ,
        "APPCARDS_TARGET_APP": app_id,
        "APPCARDS_SKIP_OPEN_APP": "1",
    }
    if capability:
        child_env["APPCARDS_FORCE_CAPABILITY"] = capability
        child_env["APPCARDS_INVOCATION_TEXT"] = goal

    cmd = [
        str(MW_BIN), "test", goal,
        "--agent-type", str(AGENT_FILE),
        "--model_name", env["LLM_MODEL"],
        "--llm_base_url", env["LLM_BASE_URL"],
        "--api_key", env["LLM_API_KEY"],
        *extra_mw_args,
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
    logger.info(
        f"catalog: {len(catalog['apps'])} apps, {len(catalog['flows'])} flows"
    )
    llm = OpenAI(base_url=env["LLM_BASE_URL"], api_key=env["LLM_API_KEY"])
    decision = route(args.nl, catalog, llm, env["LLM_MODEL"])
    print(json.dumps(decision, ensure_ascii=False, indent=2))

    if args.dry_run:
        return 0

    rec = None
    if args.record is not None:
        from datetime import datetime
        out_dir = (
            Path(args.record).expanduser().resolve()
            if args.record
            else REPO_ROOT / "traj_logs" / "recordings" / datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        rec = _recorder.start(out_dir)
        logger.info(f"screen recording → {out_dir}")

    try:
        kind = decision.get("kind")
        if kind == "flow":
            return dispatch_flow(decision, args.nl, catalog, extra)
        if kind == "app":
            return dispatch_app(decision, catalog, env, extra)
        raise SystemExit(f"router returned unknown kind={kind!r}")
    finally:
        if rec is not None:
            final = rec.stop()
            if final:
                logger.info(f"recording saved → {final}")


if __name__ == "__main__":
    sys.exit(main())
