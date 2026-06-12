"""Kotlin-callable entrypoints: run_single (one app leg) / run_flow (full NL).

Both install the Android device backend + overlay interaction, point every
output path at the app's filesDir, then run the SAME pipeline as the host
(`agents.native_runner.run_leg` / `agents.nl_flow`). Results return to
Kotlin as a JSON string.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from java import jclass
from loguru import logger

Bridge = jclass("com.relayagent.app.DeviceBridge")


def _files_dir() -> Path:
    return Path(str(Bridge.appFilesDir()))


def _install_env(cfg: dict) -> Path:
    """Install the Settings-provided LLM config + Android path layout as env
    (the runtime's existing env contract), and return this run's traj root."""
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        if cfg.get(key):
            os.environ[key] = str(cfg[key])

    files = _files_dir()
    relay = files / "relay"  # extracted by AssetInstaller
    os.environ["RELAY_MANIFESTS"] = str(relay / "manifests")
    os.environ["RELAY_TRAJ_ROOT"] = str(files / "traj_logs")
    # No OpenAI SDK in the APK; force the stdlib HTTP chat client.
    os.environ["RELAY_LLM_HTTP"] = "1"
    # Legs run in this process (Chaquopy cannot spawn subprocesses).
    os.environ["RELAY_LEG_EXECUTOR"] = "inprocess"

    run_root = files / "traj_logs" / datetime.now().strftime("%Y%m%d_%H%M%S")
    return run_root


def _bootstrap(cfg: dict) -> Path:
    from agents.interaction import set_interaction

    from relay_android.backend import install as install_backend
    from relay_android.interaction import OverlayInteraction

    run_root = _install_env(cfg)
    install_backend()
    set_interaction(OverlayInteraction())
    return run_root


def run_single(pkg: str, goal: str, config_json: str) -> str:
    """On-device `python -m agents.native_runner <pkg> <goal>`."""
    cfg = json.loads(config_json or "{}")
    run_root = _bootstrap(cfg)
    os.environ["RELAY_TRAJ_DIR"] = str(run_root / "single")
    try:
        from agents.native_runner import run_leg

        summary = run_leg(pkg, goal, max_step=int(cfg.get("max_step", -1)))
        return json.dumps({"ok": True, "summary": summary}, ensure_ascii=False)
    except Exception as e:
        logger.exception("run_single failed")
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


def run_flow(nl: str, config_json: str) -> str:
    """On-device `scripts/run_plan.py --yes` equivalent: plan + execute."""
    cfg = json.loads(config_json or "{}")
    _bootstrap(cfg)
    # Imported before the try: the except clauses below reference these names.
    from agents import nl_flow

    try:
        from agents.capability_matrix_router import load_matrix
        from agents.card_catalog import build_catalog
        from agents.flow_planner import FlowPlanner
        from agents.flow_runner import InProcessLegExecutor, _RecordingLLM
        from agents.llm_client import make_llm_client

        files = _files_dir()
        relay = files / "relay"
        catalog = build_catalog(relay / "manifests")
        matrix = load_matrix(relay / "app_capability_matrix.csv")
        llm = _RecordingLLM(
            make_llm_client(os.environ["LLM_BASE_URL"], os.environ["LLM_API_KEY"]),
            retry=False,
        )
        llm.purpose = "plan"
        # mw_fallback=False: no host MobileWorld runtime on the phone —
        # uncovered legs surface as unsatisfiable instead.
        planner = FlowPlanner(
            catalog, llm, os.environ["LLM_MODEL"], matrix=matrix, mw_fallback=False
        )

        result = nl_flow.plan_request(
            nl, planner=planner,
            generated_dir=files / "relay" / "_generated",
            allow_mw_legs=False,
        )
        if result.unsatisfiable:
            return json.dumps(
                {"ok": False, "unsatisfiable": True, "reason": result.reason},
                ensure_ascii=False,
            )
        if result.validation is not None:
            return json.dumps(
                {"ok": False, "validation_errors": result.validation.errors},
                ensure_ascii=False,
            )

        # prekill=False: prekill is force-stop based, which has no real
        # equivalent without shell (CLEAR_TASK relaunch happens per leg).
        outcome = nl_flow.execute_plan(
            result.plan_path,
            leg_executor=InProcessLegExecutor(),
            prekill=False,
        )
        return json.dumps(
            {
                "ok": True,
                "blackboard": _jsonable(outcome.blackboard),
                "traj_root": str(outcome.flow_traj_root),
            },
            ensure_ascii=False,
        )
    except nl_flow.FlowExecutionError as e:
        logger.exception("run_flow leg failed")
        return json.dumps(
            {"ok": False, "error": str(e.original), "traj_root": str(e.flow_traj_root)},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.exception("run_flow failed")
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


def _jsonable(obj):
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)
