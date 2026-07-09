"""Fallback-leg helpers for the flow planner (MobileWorld + general).

When a leg's manifest/capability routing can't cover it (or the planner judges
the whole request unsatisfiable), the leg is converted into a fallback leg
instead of failing the plan:

- **MobileWorld** (host default): handed to MW's manifest-free `general_e2e`
  agent via scripts/run_mobileworld.py.
- **General** (used when MW is off/unavailable — the on-device app, or a host
  without the `mw` extra): handed to the manifest-free GeneralGUIAgent
  (agents/agent/general_agent.py) on the SAME native runtime.

Priority is MW first, then general; both off → unsatisfiable (the pre-fallback
behavior). Split out of `flow_planner.py`. See docs/nl_flow.md "MobileWorld 兜底".
"""

from __future__ import annotations

import os

from agents.flow.flow_runner_util import GENERAL_STEP_TYPE, MW_STEP_TYPE

__all__ = [
    "MW_STEP_TYPE",
    "GENERAL_STEP_TYPE",
    "mw_fallback_enabled",
    "general_fallback_enabled",
    "_to_mw_leg",
    "_to_general_leg",
    "_mw_whole_request_plan",
    "_general_whole_request_plan",
]


def mw_fallback_enabled() -> bool:
    """Whether an uncoverable leg falls back to MobileWorld instead of failing
    the plan. Default on; disable with RELAY_MW_FALLBACK=0 (or
    `run_plan.py --no-mw-fallback`)."""
    return os.getenv("RELAY_MW_FALLBACK", "1") != "0"


def general_fallback_enabled() -> bool:
    """Whether an uncoverable leg falls back to the manifest-free general GUI
    agent when MobileWorld fallback is off/unavailable. Default on; disable
    with RELAY_GENERAL_FALLBACK=0. Strictly below MW in priority, so hosts
    with MW keep their behavior unchanged."""
    return os.getenv("RELAY_GENERAL_FALLBACK", "1") != "0"


def _to_mw_leg(step: dict, reason: str) -> dict:
    """In place, turn one synthesized app step into a MobileWorld fallback leg.

    Keeps `id` / `prompt` / `bind` / `extract` (MW's final answer text feeds the
    same blackboard slot an app leg would) and keeps `app` as a *prelaunch hint*
    only — there is no capability to route. Drops the capability requirement and
    the coverage-gap marker."""
    step["type"] = MW_STEP_TYPE
    step.pop("capability", None)
    step.pop("x_coverage_gap", None)
    # Drop the provisional route key stamped before routing failed: a MW leg is
    # not a matrix route, so it must not feed route solidification.
    step.pop("x_route_key", None)
    step["x_fallback_reason"] = reason
    return step


def _mw_whole_request_plan(nl_request: str, reason: str) -> dict:
    """A one-leg plan that hands the entire request to MobileWorld.

    Used when the planner LLM judges the request unsatisfiable outright (no
    steps to convert leg-by-leg), so per-leg fallback degenerates to a single
    MobileWorld leg carrying the original request."""
    return {
        "description": f"MobileWorld fallback: {reason}",
        "apps_required": [],
        "steps": [
            {
                "id": "mw_fallback",
                "type": MW_STEP_TYPE,
                "prompt": nl_request,
                "x_fallback_reason": reason,
            }
        ],
    }


def _to_general_leg(step: dict, reason: str) -> dict:
    """In place, turn one synthesized app step into a general-fallback leg.

    Same contract as `_to_mw_leg`: keeps `id` / `prompt` / `bind` / `extract`
    (the general agent's final answer feeds the same blackboard slot) and keeps
    `app` as a *launch hint* only. Drops the capability requirement, the
    coverage-gap marker and the provisional route key."""
    step["type"] = GENERAL_STEP_TYPE
    step.pop("capability", None)
    step.pop("x_coverage_gap", None)
    step.pop("x_route_key", None)
    step["x_fallback_reason"] = reason
    return step


def _general_whole_request_plan(nl_request: str, reason: str) -> dict:
    """A one-leg plan that hands the entire request to the general GUI agent
    (the no-MobileWorld counterpart of `_mw_whole_request_plan`)."""
    return {
        "description": f"general fallback: {reason}",
        "apps_required": [],
        "steps": [
            {
                "id": "general_fallback",
                "type": GENERAL_STEP_TYPE,
                "prompt": nl_request,
                "x_fallback_reason": reason,
            }
        ],
    }
