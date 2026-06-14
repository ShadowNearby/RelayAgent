"""MobileWorld fallback helpers for the flow planner.

When a leg's manifest/capability routing can't cover it (or the planner judges
the whole request unsatisfiable), the leg is converted into a MobileWorld
fallback leg handed to MW's manifest-free `general_e2e` agent instead of failing
the plan. Split out of `flow_planner.py`. See docs/nl_flow.md "MobileWorld 兜底".
"""

from __future__ import annotations

import os

from agents.flow.flow_runner_util import MW_STEP_TYPE

__all__ = [
    "MW_STEP_TYPE",
    "mw_fallback_enabled",
    "_to_mw_leg",
    "_mw_whole_request_plan",
]


def mw_fallback_enabled() -> bool:
    """Whether an uncoverable leg falls back to MobileWorld instead of failing
    the plan. Default on; disable with RELAY_MW_FALLBACK=0 (or
    `run_plan.py --no-mw-fallback`)."""
    return os.getenv("RELAY_MW_FALLBACK", "1") != "0"


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
