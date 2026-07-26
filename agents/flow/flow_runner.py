"""Multi-app flow runner.

Executes a flow plan (the step/bind schema produced by `FlowPlanner` and
persisted under `manifests/_generated/` by `scripts/run_plan.py`) as a
sequence of (a) native runner sub-runs pinned to one app + one capability,
(b) user-input prompts, and (c) text-LLM extract steps that parse the
last sub-run's captured reply into structured data.

Design notes (see CLAUDE.md for project context):

- Each app step is a fresh native runner subprocess. We DON'T reuse one long-
  lived RelayAgent across apps because plan cursor / chat history are
  scoped to a single card.
- The capability router is bypassed via RELAY_FORCE_CAPABILITY +
  RELAY_INVOCATION_TEXT, so each sub-run skips the routing LLM call
  and goes straight into plan building.
- The captured in-app reply is shipped from the sub-process to the parent
  via RELAY_REPLY_OUT (a JSON file written at handoff/done).
- Extract steps run a small text-only chat completion against the same
  LLM endpoint configured in `.env` (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL).
- Templating: `{var}` and `{var.field}` substitution against a flat
  blackboard dict that starts empty and grows as steps bind values
  (synthesized plans bake concrete values into prompts, so there is no
  separate `inputs` block).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from dataclasses import dataclass, field

from agents.runtime._adb import screencap
from agents.runtime.interaction import get_interaction
from agents.flow.leg_judge import LOADING, LegVerdict, final_frames, judge_leg
from agents.flow.leg_recovery import (
    ENV_FAIL,
    ROUTE_FAIL,
    TIER_GENERAL,
    TIER_MW,
    TIER_REROUTE,
    TIER_RETRY,
    LegFailure,
    RecoveryController,
    classify_leg_failure,
)
from agents.flow.flow_planner_mw import (
    _to_general_leg,
    _to_mw_leg,
    general_fallback_enabled,
    mw_fallback_enabled,
)
from agents.llm.llm_client import make_llm_client
from agents.routing.route_overlay import RouteOverlay
from agents.runtime.runtime_config import ensure_llm_env

# Pieces split out of this module; re-exported here so `flow_runner` stays the
# public facade (tests, nl_flow and the Android entry import these from here).
from agents.flow.flow_recording_llm import _RecordingLLM
from agents.flow.user_profile import load_profile, redact_obj
from agents.flow.flow_leg_executor import (
    InProcessLegExecutor,
    SubprocessLegExecutor,
    _default_leg_executor,
)
from agents.flow.flow_runner_util import (
    ENV_FILE,
    GENERAL_HOME_TARGET,
    GENERAL_STEP_TYPE,
    MW_STEP_TYPE,
    REPO_ROOT,
    RUN_MOBILEWORLD,
    _assert_output_free_step_completed,
    _harvest_mw_traj,
    _load_mw_driver,
    _parse_fenced_json,
    _read_json_file,
    _redact,
    _resolve_choice,
    _select_from_path,
    render,
)

__all__ = [
    "FlowRunner",
    "InProcessLegExecutor",
    "SubprocessLegExecutor",
    "_RecordingLLM",
    "MW_STEP_TYPE",
    "GENERAL_STEP_TYPE",
    "_harvest_mw_traj",
]


# cold-launch delegates to agents.runtime._adb so native_runner/flow_runner/relay_agent
# open_app share one implementation.


# --------------------------------------------------------------------------- #
# Leg attempt result
# --------------------------------------------------------------------------- #


@dataclass
class LegResult:
    """Everything one app-leg attempt produced, for classification/commit.

    `hard_error` carries what used to raise inline (missing needed reply /
    output-free terminal assert); `verdict` is the leg judge's advisory call
    (None when judging was disabled or errored)."""

    rc: int
    reply: str
    summary: dict[str, Any] = field(default_factory=dict)
    needs_reply: bool = False
    hard_error: str | None = None
    verdict: LegVerdict | None = None
    leg_dir: Path | None = None
    prompt: str = ""


# --------------------------------------------------------------------------- #
# FlowRunner
# --------------------------------------------------------------------------- #


class FlowRunner:
    def __init__(
        self,
        flow_path: Path,
        env_overrides: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
        leg_executor: Any | None = None,
    ) -> None:
        self.flow_path = flow_path
        self.flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
        if "steps" not in self.flow:
            raise ValueError(f"Flow {flow_path} has no `steps`")

        self.env = ensure_llm_env(ENV_FILE, env_overrides)

        self.extra_args = extra_args or []
        self._leg_executor = leg_executor or _default_leg_executor()
        # Wrapped so every flow-process LLM call (leg judge, bind extraction)
        # is recorded and later folded into each leg's traj.json — see
        # `_RecordingLLM` / `_fold_flow_llm_calls`.
        self._llm = _RecordingLLM(
            make_llm_client(self.env["LLM_BASE_URL"], self.env["LLM_API_KEY"])
        )

        # Each flow run gets its own traj root, with one dir per leg
        # (`NN_<step-id>/`) holding that leg's trajectory directly — the
        # subprocess is pointed there via RELAY_TRAJ_DIR (see _run_app_step), so
        # there is no global `traj_logs/user_task/` scratch and no `user_task/`
        # subdir. Named with the timestamp first, then the apps it touches:
        # `<ts>_plan_<app1>_<app2>...`.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        # RELAY_TRAJ_ROOT relocates the trajectory base dir (Android: REPO_ROOT
        # lives inside the read-only APK, so the app points this at filesDir).
        # Host default unchanged: <repo>/traj_logs/.
        traj_base = Path(os.getenv("RELAY_TRAJ_ROOT") or (REPO_ROOT / "traj_logs"))
        self.flow_traj_root = traj_base / f"{ts}_{self._traj_stem()}"
        self._step_idx = 0
        logger.info(f"flow traj root: {self.flow_traj_root}")

        self.bb: dict[str, Any] = {}

        # Trace-guided route solidification: each leg's verdict is folded back
        # into the overlay the planner's router reads from (see route_overlay).
        self._overlay = RouteOverlay()

        # MobileWorld fallback server: started once for the whole flow (reused
        # across MW legs) and torn down in run()'s finally — so we don't churn a
        # server per leg or leave an orphan. A server already healthy on the URL
        # is reused and left untouched. See _ensure_mw_server / _teardown_mw_server.
        self._mw_server_url = os.getenv("RELAY_MW_SERVER_URL", "http://127.0.0.1:6800")
        self._mw_server_proc: subprocess.Popen | None = None
        self._mw_server_log = None

        # Runtime failure-recovery ladder (leg_recovery; RELAY_RECOVERY=0
        # restores the old fail-fast behavior). Per-flow budgets live on the
        # controller; per-step outcomes accumulate for flow_report.json.
        self._recovery = RecoveryController(self._llm, self.env["LLM_MODEL"])
        self._step_outcomes: list[dict[str, Any]] = []

    # ------------------------------------------------------------- traj naming

    def _traj_stem(self) -> str:
        """Name for the flow-scoped traj dir.

        Named after the apps the plan touches — `plan_<app1>_<app2>...` —
        using the last segment of each leg's package id, deduped in step
        order. Falls back to the file stem if no app legs are present.
        """
        apps: list[str] = []
        for step in self.flow.get("steps", []):
            pkg = step.get("app")
            if not pkg:
                # A fallback leg with no app hint still gets a label so an
                # all-fallback flow isn't named after the file stem.
                if step.get("type") == MW_STEP_TYPE and "mw" not in apps:
                    apps.append("mw")
                elif step.get("type") == GENERAL_STEP_TYPE and "general" not in apps:
                    apps.append("general")
                continue
            short = str(pkg).rsplit(".", 1)[-1]
            if short and short not in apps:
                apps.append(short)
        return "plan_" + "_".join(apps) if apps else self.flow_path.stem

    # ------------------------------------------------------------------ run

    def run(self) -> dict[str, Any]:
        logger.info(f"FlowRunner start: {self.flow_path.name}  inputs={self.bb}")
        try:
            interaction = get_interaction()
            for step in self.flow["steps"]:
                if interaction.should_stop():
                    logger.warning("stop requested via interaction provider; ending flow early")
                    break
                kind = step.get("type") or "app_step"
                logger.info(f"--- step {step['id']!r} ({kind}) ---")
                interaction.emit_status(
                    {"event": "leg_start", "id": step["id"], "kind": kind,
                     "app": step.get("app")}
                )
                if kind == "app_step":
                    self._run_app_step(step)
                elif kind == "ask_user":
                    self._run_ask_user(step)
                elif kind == MW_STEP_TYPE:
                    self._run_mobileworld_step(step)
                elif kind == GENERAL_STEP_TYPE:
                    self._run_general_step(step)
                else:
                    raise ValueError(f"Unknown step type: {kind}")
                interaction.emit_status({"event": "leg_end", "id": step["id"]})
                logger.info(f"blackboard after {step['id']!r}: {_redact(self.bb)}")
        finally:
            # Tear down a MobileWorld server WE started (no-op if none / reused).
            self._teardown_mw_server()
            # Flow-level outcome report (per-step status + recovery attempts +
            # blackboard keys). Written on success AND on a mid-flow abort, so a
            # partially-failed flow still leaves a machine-readable account of
            # what was accomplished. Best-effort.
            self._write_flow_report()
        logger.info("FlowRunner done")
        return self.bb

    def _write_flow_report(self) -> None:
        try:
            report = {
                "flow": self.flow_path.name,
                "steps": self._step_outcomes,
                "blackboard_keys": sorted(self.bb.keys()),
                "recovery": {
                    "enabled": self._recovery.enabled,
                    "extra_legs_used": self._recovery.extra_legs_used,
                    "tokens_used": self._recovery.tokens_used,
                    "attempts": self._recovery.attempts,
                },
            }
            self.flow_traj_root.mkdir(parents=True, exist_ok=True)
            (self.flow_traj_root / "flow_report.json").write_text(
                json.dumps(redact_obj(report), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001 — reporting must never mask the run outcome
            logger.warning(f"failed to write flow_report.json: {e}")

    # ------------------------------------------------------------ app_step

    def _run_app_step(self, step: dict) -> None:
        """One plan step: execute the leg, classify the outcome, climb the
        recovery ladder on failure (RELAY_RECOVERY=0 restores fail-fast),
        commit the surviving result to the blackboard."""
        result = self._execute_app_leg(step)
        failure = classify_leg_failure(
            result.rc, result.summary, result.reply, result.needs_reply,
            result.hard_error, result.verdict,
        )
        if failure is None:
            self._commit_leg(step, result)
            self._note_outcome(step, "ok")
            return
        if not self._recovery.enabled:
            # Legacy behavior: hard failures raise; a judge-only failure was
            # already logged as a warning — commit and continue.
            if failure.fatal:
                self._note_outcome(step, "failed", failure)
                raise RuntimeError(f"Step {step['id']!r}: {failure.reason}")
            self._commit_leg(step, result)
            self._note_outcome(step, "judged_failed", failure)
            return
        self._recover_app_leg(step, result, failure)

    def _note_outcome(
        self, step: dict, status: str, failure: LegFailure | None = None,
        recovered_via: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {"step": step.get("id"), "status": status}
        if failure is not None:
            entry["failure_kind"] = failure.kind
            entry["failure_reason"] = failure.reason
        if recovered_via:
            entry["recovered_via"] = recovered_via
        self._step_outcomes.append(entry)

    def _execute_app_leg(
        self, step: dict, dir_suffix: str = "", prompt_override: str | None = None,
    ) -> LegResult:
        app = step["app"]
        capability = step["capability"]
        prompt = prompt_override or render(step["prompt"], self.bb)

        # Cold-launch is deferred to the agent's first predict
        # (RELAY_AGENT_LAUNCH below) so process/leg startup lands before the
        # launch and is excluded from the leg's task wall-clock (which the
        # agent writes to RELAY_WALL_OUT).
        self._step_idx += 1
        step_log_root = self.flow_traj_root / f"{self._step_idx:02d}_{step['id']}{dir_suffix}"
        step_log_root.mkdir(parents=True, exist_ok=True)
        # Mark where this leg's flow-process LLM calls (judge) begin in the
        # recorder buffer so we can fold exactly this attempt's slice below.
        llm_call_start = len(self._llm.calls)

        with tempfile.NamedTemporaryFile(
            mode="w+", suffix=".json", prefix="relay_reply_", delete=False
        ) as fh:
            reply_path = Path(fh.name)
        summary_path = step_log_root / "summary.json"
        try:
            # Priority: explicit overrides (the per-step RELAY_* keys
            # below) > shell env > .env file. Putting `self.env` (sourced
            # from .env) underneath `os.environ` lets a user override any
            # LLM_* / RELAY_* setting from their shell without editing
            # .env. The per-step keys at the end always win.
            child_env = {
                **self.env,
                **os.environ,
                "RELAY_TARGET_APP": app,
                "RELAY_SKIP_OPEN_APP": "1",
                "RELAY_AGENT_LAUNCH": "1",
                "RELAY_FORCE_CAPABILITY": capability,
                "RELAY_INVOCATION_TEXT": prompt,
                "RELAY_REPLY_OUT": str(reply_path),
                "RELAY_SUMMARY_OUT": str(summary_path),
                # Pin the subprocess's trajectory dir to THIS leg's dir so the
                # agent writes traj.json / steps/ / agent_reply.json (and the
                # framework-excluded wall_clock.json) straight here — no global
                # traj_logs/user_task/ scratch, no post-run copy. RELAY_TRAJ_DIR
                # also makes the native runner skip its backup rotation (each leg
                # dir is already unique). See native_runner._rotate_traj_dir.
                "RELAY_TRAJ_DIR": str(step_log_root),
                "RELAY_WALL_OUT": str(step_log_root / "wall_clock.json"),
            }
            # The native runner reads LLM_* + RELAY_* from the leg env.
            logger.info(
                f"→ native runner for app={app} capability={capability!r} prompt={prompt!r}"
            )
            # The framework-excluded per-leg wall_clock.json is written by the
            # agent (RELAY_WALL_OUT) at leg end; here we only print the
            # gross leg time for reference when RELAY_TIMING=1.
            #
            # TODO(phase-B): same-session handoff round-trip. When this leg
            # carries a `resume: true` marker, DON'T close stdin with EOF —
            # keep the subprocess alive and wire a flow⇄agent channel (a fifo
            # or file the flow writes the user's answer into) so the in-app
            # agent's handoff ask_user (see relay_agent.py) blocks on that
            # answer and resumes predict() in the SAME conversation instead of
            # terminating. Phase A handles handoff at flow granularity (a fresh
            # leg after a flow-level ask_user), which loses in-app state; phase B
            # preserves it. (The InteractionProvider in the in-process executor
            # already gives the channel; the subprocess executor needs the fifo.)
            timing = os.getenv("RELAY_TIMING", "0") == "1"
            t0 = time.monotonic()
            rc = self._leg_executor.run(app, prompt, child_env, self.extra_args)
            if timing:
                logger.info(f"leg gross wall_s={round(time.monotonic() - t0, 1)}")
            if rc != 0:
                logger.warning(f"native runner exited rc={rc}; continuing if reply was captured")

            reply = ""
            if reply_path.exists() and reply_path.stat().st_size > 0:
                payload = json.loads(reply_path.read_text(encoding="utf-8"))
                reply = (payload.get("reply") or "").strip()
            summary = _read_json_file(summary_path)
            needs_reply = bool(step.get("bind") or step.get("extract"))
            # Hard signals are captured (not raised) so the recovery ladder can
            # classify them; _run_app_step re-raises when recovery is off.
            hard_error: str | None = None
            if not reply and needs_reply:
                hard_error = (
                    f"no reply captured (needed for bind/extract). "
                    f"Check the sub-run's {step_log_root}/."
                )
            elif not needs_reply:
                try:
                    _assert_output_free_step_completed(step, summary, rc, summary_path)
                except RuntimeError as e:
                    hard_error = str(e)
            if reply:
                logger.info(f"captured reply ({len(reply)} chars) from {app}")
            else:
                logger.info(f"no reply captured for output-free step {step['id']!r}")
            # Semantic outcome check on top of the hard signals above: a leg can
            # reach a terminal state with a non-empty reply yet still not have
            # accomplished the goal. Best-effort — a judge failure must never
            # abort the flow (see leg_judge module docstring). Skipped when a
            # hard signal already failed the leg (nothing to second-guess).
            verdict: LegVerdict | None = None
            if hard_error is None:
                verdict = self._judge_leg(
                    step, app, capability, prompt, reply, step_log_root,
                    summary.get("last_action_type"),
                )
        finally:
            try:
                reply_path.unlink()
            except OSError:
                pass

        # Fold this attempt's flow-process LLM calls (leg judge) into the leg's
        # traj.json top level, alongside the in-app agent's `["0"]["llm_calls"]`.
        # Best-effort — a logging gap must not break the flow. (The bind
        # extraction call is folded by _commit_leg into the committed attempt.)
        self._fold_flow_llm_calls(step_log_root, llm_call_start)

        return LegResult(
            rc=rc, reply=reply, summary=summary, needs_reply=needs_reply,
            hard_error=hard_error, verdict=verdict, leg_dir=step_log_root,
            prompt=prompt,
        )

    def _commit_leg(self, step: dict, result: LegResult) -> None:
        """Write the surviving attempt's reply into the blackboard.

        A falsy bind (missing, null, or "") means nothing downstream consumes
        this leg — don't write it (a `bind: null` would otherwise land as a
        None key in the blackboard)."""
        if not step.get("bind"):
            return
        llm_call_start = len(self._llm.calls)
        if "extract" in step:
            value = self._extract(result.reply, step["extract"])
        else:
            value = result.reply
        self.bb[step["bind"]] = value
        if result.leg_dir is not None:
            self._fold_flow_llm_calls(result.leg_dir, llm_call_start)

    # ------------------------------------------------------- recovery ladder

    def _recover_app_leg(
        self, step: dict, first_result: LegResult, first_failure: LegFailure,
    ) -> None:
        """Climb the recovery ladder for a failed leg: retry → reroute →
        MobileWorld fallback → partial-success terminal. See leg_recovery for
        the taxonomy, tier policy and budgets."""
        rec = self._recovery
        cur_step = step
        result, failure = first_result, first_failure
        original_dir = first_result.leg_dir
        exclude: set[tuple[str, str]] = {(step["app"], step["capability"])}
        # Safety red line: handoff-required capabilities get the retry tier
        # only — never a different app (would redo user-visible prep), never
        # MobileWorld (general_e2e has no handoff contract; it could cross an
        # irreversible action on its own).
        handoff_leg = rec.handoff_required(step["app"], step["capability"])
        retries_used = 0
        reroute_tried = False
        attempts: list[dict[str, Any]] = []
        committed = False

        def _log_attempt(
            tier: str, target: str, outcome: str, detail: str, tokens: int = 0,
        ) -> None:
            entry = {
                "step": step.get("id"), "tier": tier, "target": target,
                "outcome": outcome, "detail": detail, "tokens": tokens,
            }
            attempts.append(entry)
            rec.record(entry)
            logger.info(f"recovery [{tier}] {target}: {outcome} — {detail}")

        while failure is not None:
            if failure.kind == ENV_FAIL:
                logger.warning(f"leg {step['id']!r} failed at the environment layer; not recoverable")
                break
            if retries_used < rec.max_retries:
                tier = TIER_RETRY
            elif handoff_leg:
                break  # retry-only ladder for handoff capabilities
            elif not reroute_tried:
                tier = TIER_REROUTE
            elif mw_fallback_enabled():
                tier = TIER_MW
            elif general_fallback_enabled():
                # No MobileWorld runtime (on-device / no mw extra): the last
                # tier is the manifest-free general agent on the native runtime.
                tier = TIER_GENERAL
            else:
                break
            if not rec.can_spend_leg():
                break

            if tier == TIER_RETRY:
                retries_used += 1
                mark = len(self._llm.calls)
                override = None
                if failure.kind == ROUTE_FAIL:
                    override = rec.reword(
                        result.prompt, failure, cur_step["app"], cur_step["capability"]
                    )
                if original_dir is not None:
                    self._fold_flow_llm_calls(original_dir, mark)
                new_result = self._execute_app_leg(
                    cur_step, dir_suffix=f"_retry{retries_used}", prompt_override=override,
                )
                spent = rec.spend_leg(new_result.summary)
                target = f"{cur_step['app']}/{cur_step['capability']}"
            elif tier == TIER_REROUTE:
                reroute_tried = True
                mark = len(self._llm.calls)
                decision = rec.reroute(result.prompt, exclude)
                if original_dir is not None:
                    self._fold_flow_llm_calls(original_dir, mark)
                if decision is None:
                    _log_attempt(tier, "-", "skipped", "no alternative route")
                    continue  # fall through to the next tier on the next pass
                cur_step = {
                    **cur_step,
                    "app": decision["app_id"],
                    "capability": decision["capability_id"],
                }
                new_result = self._execute_app_leg(cur_step, dir_suffix="_reroute")
                spent = rec.spend_leg(new_result.summary)
                target = f"{cur_step['app']}/{cur_step['capability']}"
            else:  # TIER_MW / TIER_GENERAL — the last tier either way
                to_leg = _to_mw_leg if tier == TIER_MW else _to_general_leg
                label = "mobileworld" if tier == TIER_MW else "general"
                fb_step = to_leg(
                    {**cur_step},
                    f"runtime recovery: {failure.kind} — {failure.reason}",
                )
                rec.spend_leg(None)
                try:
                    # Runs, judges, binds and folds itself (same path as a
                    # plan-time fallback leg) — nothing left to commit here.
                    if tier == TIER_MW:
                        self._run_mobileworld_step(fb_step)
                    else:
                        self._run_general_step(fb_step)
                except Exception as e:  # noqa: BLE001 — last tier
                    _log_attempt(tier, label, "failed", str(e))
                    break
                _log_attempt(tier, label, "ok", f"leg completed via {label} fallback")
                committed = True
                self._note_outcome(step, "recovered", first_failure, recovered_via=tier)
                break

            new_failure = classify_leg_failure(
                new_result.rc, new_result.summary, new_result.reply,
                new_result.needs_reply, new_result.hard_error, new_result.verdict,
            )
            if new_failure is None:
                _log_attempt(tier, target, "ok", "attempt succeeded", tokens=spent)
                self._commit_leg(cur_step, new_result)
                committed = True
                self._note_outcome(step, "recovered", first_failure, recovered_via=tier)
                break
            _log_attempt(tier, target, "failed",
                         f"{new_failure.kind}: {new_failure.reason}", tokens=spent)
            exclude.add((cur_step["app"], cur_step["capability"]))
            result, failure = new_result, new_failure

        # Persist the attempt log next to the original attempt's trajectory.
        if attempts and original_dir is not None:
            try:
                (original_dir / "recovery.json").write_text(
                    json.dumps(attempts, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError as e:
                logger.warning(f"failed to write recovery.json: {e}")

        if committed:
            return
        # Ladder exhausted. A fatal failure stops the flow (with the partial
        # state accounted for in flow_report.json); a judge-only failure keeps
        # today's semantics — ship the best attempt and continue.
        if failure is not None and failure.fatal:
            self._note_outcome(step, "failed", failure)
            raise RuntimeError(
                f"Step {step['id']!r}: {failure.reason} "
                f"(recovery exhausted after {len(attempts)} attempt(s); "
                f"partial results in {self.flow_traj_root / 'flow_report.json'})"
            )
        self._commit_leg(cur_step, result)
        self._note_outcome(step, "judged_failed", failure)

    # ------------------------------------------------------ mobileworld leg

    def _run_mobileworld_step(self, step: dict) -> None:
        """Execute a fallback leg through MobileWorld's manifest-free general_e2e
        agent — for a leg RA's manifest/capability routing could not cover.

        Shells out to scripts/run_mobileworld.py (which owns the MW server
        lifecycle, prelaunch and .env LLM config), then harvests MobileWorld's
        final `answer` text as the leg reply so the SAME blackboard bind/extract,
        leg-judge and traj-fold paths as an app leg apply."""
        prompt = render(step["prompt"], self.bb)

        self._step_idx += 1
        step_log_root = self.flow_traj_root / f"{self._step_idx:02d}_{step['id']}"
        step_log_root.mkdir(parents=True, exist_ok=True)
        llm_call_start = len(self._llm.calls)
        # `app` on a MW leg is only a prelaunch hint, not a routed app.
        app_hint = step.get("app")
        summary_path = step_log_root / "summary.json"

        # One server for the whole flow (started here on the first MW leg, reused
        # after) so we don't start/stop a server per leg or orphan one.
        self._ensure_mw_server()

        max_round = os.getenv("RELAY_MW_MAX_ROUND", "25")
        timeout = os.getenv("RELAY_MW_TIMEOUT", "600")
        cmd = [
            sys.executable, str(RUN_MOBILEWORLD), prompt,
            "--agent-type", "general_e2e",
            "--max-round", str(max_round),
            "--timeout", str(timeout),
            # Use the flow-managed server; never let the per-leg driver start or
            # kill its own (that's what orphaned servers before).
            "--no-start-server",
            "--server-url", self._mw_server_url,
            # Forwarded through run_mobileworld's `extra` to `mw test`, so
            # MobileWorld's TrajLogger writes <leg_dir>/user_task/traj.json.
            "--output", str(step_log_root),
        ]
        if app_hint:
            cmd += ["--app", app_hint]
        else:
            cmd += ["--no-prelaunch"]

        child_env = {**self.env, **os.environ}
        logger.info(
            f"→ MobileWorld fallback leg {step['id']!r} "
            f"(reason: {step.get('x_fallback_reason')!r}) prompt={prompt!r}"
        )
        timing = os.getenv("RELAY_TIMING", "0") == "1"
        t0 = time.monotonic()
        rc = subprocess.call(cmd, cwd=REPO_ROOT, env=child_env, stdin=subprocess.DEVNULL)
        if timing:
            logger.info(f"mw leg gross wall_s={round(time.monotonic() - t0, 1)}")
        if rc != 0:
            logger.warning(f"MobileWorld leg exited rc={rc}; continuing if a reply was captured")

        # Harvest MobileWorld's trajectory: the last `answer` action's text is the
        # leg reply; the last action overall gives the terminal signal.
        reply, terminal_action, goal_status = _harvest_mw_traj(step_log_root)
        # Persist the reply where downstream tooling expects it (mirrors the
        # native runner's agent_reply.json), and a minimal summary.json so the
        # output-free terminal check below reads a uniform shape.
        if reply:
            (step_log_root / "agent_reply.json").write_text(
                json.dumps({"reply": reply, "target_app": app_hint or "mobileworld"},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        summary = {"last_action_type": terminal_action,
                   "last_goal_status": goal_status,
                   "via": "mobileworld"}
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        needs_reply = bool(step.get("bind") or step.get("extract"))
        if not reply and needs_reply:
            raise RuntimeError(
                f"MobileWorld leg {step['id']!r}: no answer captured. "
                f"Check {step_log_root}/user_task/."
            )
        if not needs_reply:
            # Same terminal check an output-free app leg gets in
            # _execute_app_leg: with no bind to miss, a timed-out/crashed MW
            # run (rc!=0, no answer, no terminal action) would otherwise fall
            # through and read as leg success.
            _assert_output_free_step_completed(step, summary, rc, summary_path)
        if reply:
            logger.info(f"captured MobileWorld answer ({len(reply)} chars)")
        else:
            logger.info(f"no answer captured for output-free MobileWorld leg {step['id']!r}")

        # Best-effort semantic check (reads the final screen). MW success def is
        # the same as a non-handoff app leg.
        self._judge_leg(
            step, app_hint or MW_STEP_TYPE, "fallback", prompt, reply,
            step_log_root, terminal_action,
        )

        if step.get("bind"):
            if "extract" in step:
                value = self._extract(reply, step["extract"])
            else:
                value = reply
            self.bb[step["bind"]] = value

        self._fold_flow_llm_calls(step_log_root, llm_call_start)

    # -------------------------------------------------------- general leg

    def _run_general_step(self, step: dict) -> None:
        """Execute a fallback leg with the manifest-free general GUI agent
        (agents/agent/general_agent.py) on the SAME native runtime — the
        no-MobileWorld fallback (on-device, or a host without the mw extra).

        Mirrors `_run_mobileworld_step`'s contract: `app` is only a launch
        hint (absent → the agent starts from HOME and finds an app itself);
        the agent's final `answer` text is the leg reply, feeding the same
        blackboard bind/extract, leg-judge and traj-fold paths as an app leg."""
        prompt = render(step["prompt"], self.bb)

        self._step_idx += 1
        step_log_root = self.flow_traj_root / f"{self._step_idx:02d}_{step['id']}"
        step_log_root.mkdir(parents=True, exist_ok=True)
        llm_call_start = len(self._llm.calls)
        app_hint = step.get("app")
        target = app_hint or GENERAL_HOME_TARGET
        summary_path = step_log_root / "summary.json"

        with tempfile.NamedTemporaryFile(
            mode="w+", suffix=".json", prefix="relay_reply_", delete=False
        ) as fh:
            reply_path = Path(fh.name)
        try:
            child_env = {
                **self.env,
                **os.environ,
                "RELAY_TARGET_APP": target,
                "RELAY_SKIP_OPEN_APP": "1",
                "RELAY_AGENT_LAUNCH": "1",
                # The general agent module; on packaged runtimes (Chaquopy) the
                # file path is absent and native_runner._agent_spec falls back
                # to the agents.agent.general_agent module spec.
                "RELAY_AGENT_FILE": str(REPO_ROOT / "agents" / "agent" / "general_agent.py"),
                "RELAY_REPLY_OUT": str(reply_path),
                "RELAY_SUMMARY_OUT": str(summary_path),
                "RELAY_TRAJ_DIR": str(step_log_root),
                "RELAY_WALL_OUT": str(step_log_root / "wall_clock.json"),
            }
            max_step = os.getenv("RELAY_GENERAL_MAX_STEP", "25")
            logger.info(
                f"→ general fallback leg {step['id']!r} target={target} "
                f"(reason: {step.get('x_fallback_reason')!r}) prompt={prompt!r}"
            )
            timing = os.getenv("RELAY_TIMING", "0") == "1"
            t0 = time.monotonic()
            # Appended after self.extra_args so this cap wins an argparse tie.
            rc = self._leg_executor.run(
                target, prompt, child_env, [*self.extra_args, "--max-step", str(max_step)]
            )
            if timing:
                logger.info(f"general leg gross wall_s={round(time.monotonic() - t0, 1)}")
            if rc != 0:
                logger.warning(f"general leg exited rc={rc}; continuing if a reply was captured")

            reply = ""
            if reply_path.exists() and reply_path.stat().st_size > 0:
                payload = json.loads(reply_path.read_text(encoding="utf-8"))
                reply = (payload.get("reply") or "").strip()
            summary = _read_json_file(summary_path)
        finally:
            try:
                reply_path.unlink()
            except OSError:
                pass

        needs_reply = bool(step.get("bind") or step.get("extract"))
        if not reply and needs_reply:
            raise RuntimeError(
                f"General fallback leg {step['id']!r}: no answer captured. "
                f"Check {step_log_root}/."
            )
        if not needs_reply:
            # Same terminal check as an output-free app leg / MW leg — the
            # general agent runs on the native runtime, so the summary shape
            # is already the one the assert reads.
            _assert_output_free_step_completed(step, summary, rc, summary_path)
        if reply:
            logger.info(f"captured general-fallback answer ({len(reply)} chars)")
        else:
            logger.info(f"no answer captured for output-free general leg {step['id']!r}")

        # Best-effort semantic check — same success definition as an MW leg.
        self._judge_leg(
            step, app_hint or GENERAL_STEP_TYPE, "fallback", prompt, reply,
            step_log_root, summary.get("last_action_type"),
        )

        if step.get("bind"):
            if "extract" in step:
                value = self._extract(reply, step["extract"])
            else:
                value = reply
            self.bb[step["bind"]] = value

        self._fold_flow_llm_calls(step_log_root, llm_call_start)

    # ----------------------------------------------- mobileworld server

    def _ensure_mw_server(self) -> None:
        """Ensure one MobileWorld server is reachable for this flow.

        Reuses a healthy server already on `self._mw_server_url` (left untouched
        on teardown); otherwise starts one ONCE and remembers it so run()'s
        finally can stop it. Reuses run_mobileworld.py's helpers so server
        startup logic lives in one place."""
        if self._mw_server_proc is not None:
            return  # already started by us this run
        mw = _load_mw_driver()
        if mw._server_health_ok(self._mw_server_url):
            logger.info(f"reusing MobileWorld server at {self._mw_server_url}")
            return
        mw_cmd, mw_cwd = mw._resolve_mobileworld_runtime("auto", None)
        log_path = REPO_ROOT / "artifacts" / "mobileworld_server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"starting MobileWorld server → {self._mw_server_url} (log {log_path})")
        self._mw_server_log = log_path.open("ab")
        self._mw_server_proc = subprocess.Popen(
            [*mw_cmd, "server"], cwd=mw_cwd,
            stdin=subprocess.DEVNULL, stdout=self._mw_server_log,
            stderr=subprocess.STDOUT,
        )
        if not mw._wait_for_server(self._mw_server_url):
            self._teardown_mw_server()
            raise RuntimeError(
                f"MobileWorld server did not become healthy; see {log_path}"
            )
        logger.info(f"MobileWorld server healthy (pid={self._mw_server_proc.pid})")

    def _teardown_mw_server(self) -> None:
        """Stop a MobileWorld server WE started. No-op if none / reused."""
        proc = self._mw_server_proc
        if proc is not None:
            logger.info(f"stopping MobileWorld server (pid={proc.pid})")
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            self._mw_server_proc = None
        if self._mw_server_log is not None:
            self._mw_server_log.close()
            self._mw_server_log = None

    # -------------------------------------------------- flow-call folding

    def _fold_flow_llm_calls(self, leg_dir: Path, start_idx: int) -> None:
        """Append this leg's buffered flow-process LLM calls (recorded by
        `_RecordingLLM`) to the leg's traj.json under the top-level
        `flow_llm_calls` key — distinct from the in-app agent's
        `["0"]["llm_calls"]`. Best-effort: never raises."""
        calls = self._llm.calls[start_idx:]
        if not calls:
            return
        traj_path = leg_dir / "traj.json"
        try:
            data: Any = {}
            if traj_path.exists():
                data = json.loads(traj_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                logger.warning(
                    f"leg traj.json is not an object, skipping flow-call fold: {traj_path}"
                )
                return
            # M4 — RELAY_TRAJ_REDACT=1 strips profile values from the persisted
            # calls (prompts carry them by design; the traj must not).
            data.setdefault("flow_llm_calls", []).extend(redact_obj(calls))
            traj_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8"
            )
            logger.info(f"folded {len(calls)} flow LLM call(s) into {traj_path}")
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"failed to fold flow LLM calls into {traj_path}: {e}")

    # ------------------------------------------------------------ leg judge

    def _judge_leg(
        self,
        step: dict,
        app: str,
        capability: str,
        prompt: str,
        reply: str,
        step_log_root: Path,
        terminal_action: str | None,
    ) -> LegVerdict | None:
        """VLM success/failure check for a finished leg. Best-effort: logs the
        verdict, persists it next to the leg trajectory and returns it for the
        recovery ladder; never raises (returns None instead)."""
        if os.getenv("RELAY_LEG_JUDGE", "1") != "1":
            return None
        self._llm.purpose = "leg_judge"
        try:
            leg_dir = step_log_root
            frames = final_frames(leg_dir)

            def judge(fr, live=None):
                return judge_leg(
                    llm=self._llm,
                    model=self.env["LLM_MODEL"],
                    goal=prompt,
                    app=app,
                    capability=capability,
                    reply=reply,
                    frames=fr,
                    live_image=live,
                    terminal_action=terminal_action,
                )

            # With per-step logging off (RELAY_STEP_LOG=0 — the benchmark
            # default) there are no step PNGs, which used to blind the judge
            # entirely ("no frames to judge" → unknown) and with it every
            # judge-driven recovery tier. The leg has only just ended and its
            # app is still foreground, so judge a freshly captured live frame
            # instead — same pattern as the loading-retry path below.
            live0 = None
            if not frames:
                live0 = screencap()
            verdict = judge(frames, live0)
            # `loading` means the screen was still in flight (e.g. a map spinning
            # up after live_navigation's CTA), not a real outcome. Give it a
            # moment and re-judge against a FRESHLY captured frame — only on
            # loading, so the common case never pays this cost. The sub-run has
            # exited but its app is still foreground, so screencap() sees the
            # current state. Stop as soon as it settles.
            retries = int(os.getenv("RELAY_LEG_JUDGE_LOADING_RETRIES", "3"))
            wait = float(os.getenv("RELAY_LEG_JUDGE_LOADING_WAIT", "2.0"))
            ctx = frames[-1:]  # one step frame for context alongside the live one
            while verdict.status == LOADING and retries > 0:
                retries -= 1
                if wait > 0:
                    time.sleep(wait)
                live = screencap()
                if live is None:  # capture failed — keep the loading verdict
                    break
                verdict = judge(ctx, live)
            (leg_dir / "leg_verdict.json").write_text(
                json.dumps(
                    {"step": step["id"], "app": app, "capability": capability,
                     **verdict.to_dict()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            # Close the trace loop: fold this verdict into the route overlay so a
            # repeatedly-successful route gets solidified (and a failing one
            # paused) for the next run. `x_route_key` was stamped by the planner.
            # MobileWorld fallback legs carry no route key (they're not a matrix
            # route), so they are not solidified.
            route_key = step.get("x_route_key")
            if route_key:
                self._overlay.record(
                    route_key,
                    prompt or step.get("prompt", ""),
                    app,
                    capability,
                    verdict.status,
                )
            if verdict.judged and not verdict.success:
                logger.warning(
                    f"leg {step['id']!r} ({app}/{capability}) judged FAILED: {verdict.reason}"
                )
            return verdict
        except Exception as e:  # judging is advisory — never break the flow
            logger.warning(f"leg judge errored for {step.get('id')!r}: {e}")
            return None

    # ---------------------------------------------------------- ask_user

    def _run_ask_user(self, step: dict) -> None:
        header = render(step.get("prompt_header", ""), self.bb)
        bind = step["bind"]
        interaction = get_interaction()

        if "select_from" in step:
            # Same normalization as the planner's validator (shared helper),
            # so any select_from spelling that validates also runs.
            path = _select_from_path(step["select_from"])
            arr_key = ".".join(path)
            value: Any = self.bb
            for part in path:
                value = value.get(part) if isinstance(value, dict) else None
            items = value or []
            if not items:
                raise RuntimeError(f"ask_user {step['id']!r}: nothing in {arr_key!r} to choose from")
            label_tpl = step.get("item_label", "{name}")
            labels = [render(label_tpl, it) for it in items]
            # M2③ — pre-select the previous choice: when the profile remembers
            # a pick for this question (keyed by the UNrendered header, stable
            # across runs of the same/cached plan) and it is on today's list,
            # the empty default moves there. Any explicit input still wins.
            profile = load_profile()
            choice_key = step.get("prompt_header") or step.get("id") or header
            default_idx = 1
            if profile is not None:
                remembered = profile.get_choice(choice_key)
                if remembered in labels:
                    default_idx = labels.index(remembered) + 1
            lines = [header]
            lines += [f"  {i}. {label}" for i, label in enumerate(labels, 1)]
            lines.append(f"  (1-{len(items)}, or empty to pick {default_idx})")
            # ask_user → None (EOF / take-over) keeps the empty-default pick.
            raw = (interaction.ask_user("\n".join(lines)) or "").strip() or str(default_idx)
            chosen = _resolve_choice(raw, items, label_tpl)
            logger.info(f"user chose: {chosen}")
            if profile is not None:
                # Records the user's own explicit pick (not an inference) so
                # the next run of this question defaults to it.
                idx = items.index(chosen) if chosen in items else None
                if idx is not None:
                    profile.remember_choice(choice_key, labels[idx])
            self.bb[bind] = chosen
            return

        # plain freeform input
        raw = (interaction.ask_user(header) or "").strip()
        self.bb[bind] = raw

    # ----------------------------------------------------------- extract

    def _extract(self, raw_text: str, spec: dict) -> Any:
        prompt = render(spec["prompt"], self.bb)
        system = (
            "You extract structured data from text. "
            "Reply with ONE JSON value inside a ```json``` fence. "
            "No prose outside the fence."
        )
        user = f"{prompt}\n\n文本：\n{raw_text}"
        logger.info(f"extract LLM call ({len(user)} chars of text)")
        self._llm.purpose = "bind_extract"
        resp = self._llm.chat.completions.create(
            model=self.env["LLM_MODEL"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=1024,
        )
        out = (resp.choices[0].message.content or "").strip()
        logger.debug(f"extract raw reply: {out}")
        data = _parse_fenced_json(out)
        if "bind_to_array_key" in spec and isinstance(data, dict):
            data = data.get(spec["bind_to_array_key"], data)
        return data
