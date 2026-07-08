"""NL → flow pipeline, importable — plan_request + execute_plan.

The reusable core of scripts/run_plan.py, extracted so the Android app (a
Chaquopy entrypoint, no CLI) and the host CLI run the SAME pipeline:

    plan_request()  — cache lookup → (re-route + validate | synthesize) →
                      persist. Returns a structured PlanResult instead of
                      printing/exiting, so each frontend renders outcomes its
                      own way (terminal text vs. a "cannot handle" card).
    execute_plan()  — pre-kill the plan's apps → FlowRunner.run(). Raises
                      FlowExecutionError carrying the flow traj root so the
                      caller can still harvest token/timing logs on failure.

CLI-only concerns (argparse, preview rendering, y/N confirm, screen
recording, the token report) stay in scripts/run_plan.py.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from agents.flow.flow_planner import FlowPlanner, PlanValidationError
from agents.flow.flow_runner import MW_STEP_TYPE, FlowRunner

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GENERATED_DIR = REPO_ROOT / "manifests" / "_generated"


# --------------------------------------------------------------------------- #
# cache (exact normalized-request match). Semantic reuse is a TODO.
# --------------------------------------------------------------------------- #


def normalize_request(req: str) -> str:
    return " ".join((req or "").split()).strip()


def _plan_filename(req: str) -> str:
    norm = normalize_request(req)
    h = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]
    slug = re.sub(r"\W+", "_", norm, flags=re.U).strip("_")[:24] or "plan"
    return f"{slug}_{h}.yaml"


def cache_lookup(req: str, generated_dir: Path = GENERATED_DIR) -> Path | None:
    """Return a persisted plan whose source_request matches `req` exactly."""
    norm = normalize_request(req)
    if not generated_dir.exists():
        return None
    for path in sorted(generated_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if normalize_request(str(doc.get("source_request", ""))) == norm:
            # TODO(semantic-cache): fall back to embedding / LLM similarity
            # here when exact match misses, instead of regenerating.
            return path
    return None


def persist_plan(plan: dict, req: str, generated_dir: Path = GENERATED_DIR) -> Path:
    generated_dir.mkdir(parents=True, exist_ok=True)
    norm = normalize_request(req)
    ordered = {
        "flow_id": plan.get("flow_id") or ("gen_" + hashlib.sha1(norm.encode()).hexdigest()[:8]),
        "source_request": norm,
        "description": plan.get("description", ""),
        "apps_required": plan.get("apps_required", []),
        "steps": plan["steps"],
    }
    path = generated_dir / _plan_filename(req)
    path.write_text(
        yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return path


def plan_has_mw_legs(plan: dict) -> bool:
    return any(s.get("type") == MW_STEP_TYPE for s in plan.get("steps", []))


# --------------------------------------------------------------------------- #
# plan_request
# --------------------------------------------------------------------------- #


@dataclass
class PlanResult:
    """Outcome of plan_request. Exactly one of these states holds:

    - ok: `plan` + `plan_path` set, ready to execute.
    - unsatisfiable: no app coverage (or MW legs disallowed); `reason` says why.
    - validation failed: `validation` carries the PlanValidationError
      (`from_cache` distinguishes the cached-reroute vs. fresh-synthesis
      message the CLI prints).
    """

    plan: dict[str, Any] | None = None
    plan_path: Path | None = None
    from_cache: bool = False
    unsatisfiable: bool = False
    reason: str | None = None
    validation: PlanValidationError | None = None

    @property
    def ok(self) -> bool:
        return (
            self.plan_path is not None
            and not self.unsatisfiable
            and self.validation is None
        )


def plan_request(
    nl: str,
    *,
    planner: FlowPlanner,
    generated_dir: Path = GENERATED_DIR,
    use_cache: bool = True,
    allow_mw_legs: bool = True,
) -> PlanResult:
    """Resolve `nl` to an executable plan file (cache first, else synthesize).

    `allow_mw_legs=False` (the Android runtime — no host MobileWorld) turns a
    cached plan containing MobileWorld fallback legs into an unsatisfiable
    result instead of executing it. Freshly synthesized plans are governed by
    the planner's own mw_fallback flag.
    """
    if use_cache:
        hit = cache_lookup(nl, generated_dir)
        if hit:
            plan = yaml.safe_load(hit.read_text(encoding="utf-8")) or {}
            if plan.get("unsatisfiable"):
                return PlanResult(
                    plan=plan, from_cache=True, unsatisfiable=True,
                    reason=plan.get("reason"),
                )
            if not allow_mw_legs and plan_has_mw_legs(plan):
                return PlanResult(
                    plan=plan, from_cache=True, unsatisfiable=True,
                    reason="cached plan requires MobileWorld fallback legs, "
                           "which this runtime cannot execute",
                )
            try:
                plan = planner.resolve_app_routes(plan, nl)
                planner.validate_plan(plan, nl)
            except PlanValidationError as e:
                logger.error(str(e))
                return PlanResult(from_cache=True, validation=e)
            plan_path = persist_plan(plan, nl, generated_dir)
            logger.info(f"cache hit → {hit.name}")
            return PlanResult(plan=plan, plan_path=plan_path, from_cache=True)

    try:
        plan = planner.plan(nl)
    except PlanValidationError as e:
        logger.error(str(e))
        return PlanResult(validation=e)
    if plan.get("unsatisfiable"):
        return PlanResult(plan=plan, unsatisfiable=True, reason=plan.get("reason"))
    plan_path = persist_plan(plan, nl, generated_dir)
    logger.info(f"plan persisted → {plan_path}")
    return PlanResult(plan=plan, plan_path=plan_path)


# --------------------------------------------------------------------------- #
# execute_plan
# --------------------------------------------------------------------------- #


def plan_packages(plan: dict) -> list[str]:
    """Every distinct app package the plan's steps will open, in step order."""
    pkgs: list[str] = []
    for step in plan.get("steps", []):
        pkg = step.get("app")
        if pkg and pkg not in pkgs:
            pkgs.append(pkg)
    return pkgs


def prekill_apps(plan: dict) -> None:
    """Force-stop every app the plan touches before execution starts.

    Each leg's first predict still cold-launches its own app (force-stop +
    relaunch), but that only clears the leg's own app at the moment it opens —
    apps used in later legs (and the one handed off from) keep running in the
    background with stale state. Killing them all up front gives the whole plan
    a clean slate. Best-effort: a kill failure must not block the run.
    Disable with RELAY_PREKILL_APPS=0.
    """
    import os

    from agents.runtime import _adb

    if os.getenv("RELAY_PREKILL_APPS", "1") == "0":
        return
    pkgs = plan_packages(plan)
    if not pkgs:
        return
    logger.info(f"pre-kill background for {len(pkgs)} app(s): {', '.join(pkgs)}")
    for pkg in pkgs:
        try:
            _adb.force_stop(pkg)
        except Exception as e:  # noqa: BLE001 — best-effort; never block the run
            logger.warning(f"pre-kill force-stop {pkg} failed: {e}")


@dataclass
class FlowOutcome:
    blackboard: dict[str, Any]
    flow_traj_root: Path


class FlowExecutionError(RuntimeError):
    """A flow leg failed. Carries the flow traj root (already populated with
    the completed legs' logs) and chains the original exception, so callers
    can harvest token/timing accounting before re-raising/reporting."""

    def __init__(self, original: BaseException, flow_traj_root: Path) -> None:
        super().__init__(str(original))
        self.original = original
        self.flow_traj_root = flow_traj_root


def execute_plan(
    plan_path: Path,
    *,
    extra_args: list[str] | None = None,
    leg_executor: Any | None = None,
    prekill: bool = True,
    nl_request: str | None = None,
) -> FlowOutcome:
    """Run a persisted plan through FlowRunner. Returns the final blackboard +
    flow traj root; wraps leg failures in FlowExecutionError (traj root
    attached) so accounting survives.

    With `nl_request` set, a successful flow ends with the P3-M3 memory pass:
    one cheap LLM call proposes a stable preference (or nothing), and the user
    is ASKED before anything is written to the profile — never silently."""
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8")) or {}
    if prekill:
        prekill_apps(plan)
    runner = FlowRunner(
        flow_path=plan_path, extra_args=extra_args or [], leg_executor=leg_executor
    )
    try:
        bb = runner.run()
    except Exception as e:
        raise FlowExecutionError(e, runner.flow_traj_root) from e
    if nl_request:
        _maybe_remember_preference(runner, nl_request, bb)
    return FlowOutcome(blackboard=bb, flow_traj_root=runner.flow_traj_root)


def _maybe_remember_preference(runner: Any, nl_request: str, bb: dict[str, Any]) -> None:
    """P3-M3: propose-then-ask memory write after a successful flow.

    Best-effort end to end: a proposal failure, an EOF'd stdin (batch runs) or
    a non-"y" answer all mean "don't write". Gated by the same RELAY_PROFILE
    switch as the rest of the layer."""
    from agents.flow.user_profile import load_profile, profile_enabled, propose_memory
    from agents.runtime.interaction import get_interaction

    if not profile_enabled():
        return
    try:
        proposal = propose_memory(runner._llm, runner.env["LLM_MODEL"], nl_request, bb)
        if proposal is None:
            return
        key, value = proposal
        answer = get_interaction().ask_user(
            f"记住这个偏好供以后使用吗?{key} = {value} [y/N]"
        )
        if (answer or "").strip().lower() not in ("y", "yes", "是"):
            logger.info(f"memory proposal declined: {key}={value!r}")
            return
        profile = load_profile()
        if profile is None:  # enabled but no file yet — create the store
            from agents.flow.user_profile import UserProfile, profile_path

            profile = UserProfile({"version": 1}, profile_path())
        profile.add_preference(key, value)
        logger.info(f"profile remembered: {key}={value!r} → {profile.path}")
    except Exception as e:  # noqa: BLE001 — memory must never fail the flow
        logger.warning(f"memory pass skipped: {e}")
