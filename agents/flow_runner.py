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

import importlib.util
import json
import os
import re
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
from openai import OpenAI

from agents._adb import screencap
from agents.action_model import ANSWER, ASK_USER, FINISHED
from agents.leg_judge import LOADING, final_frames, judge_leg
from agents.llm_retry import create_with_retry
from agents.route_overlay import RouteOverlay
from agents.runtime_config import ensure_llm_env

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
# Each app leg is a fresh native runner subprocess (direct adb).
NATIVE_RUNNER_MODULE = "agents.native_runner"
# MobileWorld fallback legs shell out to this driver (manages the MW server,
# prelaunch, .env LLM config) — see scripts/run_mobileworld.py.
RUN_MOBILEWORLD = REPO_ROOT / "scripts" / "run_mobileworld.py"
MW_STEP_TYPE = "mobileworld"


# cold-launch delegates to agents._adb so native_runner/flow_runner/relay_agent
# open_app share one implementation.


# --------------------------------------------------------------------------- #
# templating
# --------------------------------------------------------------------------- #

_VAR_RE = re.compile(r"\{([a-zA-Z_][\w.]*)\}")


def render(template: str, ctx: dict[str, Any]) -> str:
    """Substitute `{var}` and `{var.field}` against ctx. Missing keys → ''."""
    def repl(m: re.Match) -> str:
        path = m.group(1).split(".")
        v: Any = ctx
        for p in path:
            if isinstance(v, dict):
                v = v.get(p, "")
            else:
                v = getattr(v, p, "")
        return "" if v is None else str(v)
    return _VAR_RE.sub(repl, template)


# --------------------------------------------------------------------------- #
# Flow-process LLM-call recording
# --------------------------------------------------------------------------- #


def _sanitize_flow_messages(messages: list[dict]) -> list[dict]:
    """Strip giant base64 image_url payloads (leg-judge screenshots) so the
    folded traj.json stays readable; text content is left untouched."""
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        parts: list[Any] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if isinstance(url, str) and url.startswith("data:"):
                    parts.append({"type": "image_url", "image_url": {
                        "url": f"<base64 image, {len(url)} chars>"
                    }})
                    continue
            parts.append(part)
        out.append({**msg, "content": parts})
    return out


class _RecordingLLM:
    """Thin proxy over the OpenAI client that records every
    `chat.completions.create` call (sanitized) into `self.calls`. FlowRunner
    folds each leg's slice of that buffer into the leg's traj.json under the
    top-level `flow_llm_calls` key, so flow-process LLM cost (leg judge, bind
    extraction) is observable alongside the in-app agent's
    `["0"]["llm_calls"]`. The real response object is returned untouched.

    `purpose` is a caller-set label (e.g. "leg_judge", "bind_extract") stamped
    onto each recorded call; single-threaded flow so a plain attribute is enough.

    `retry=True` (FlowRunner's callers invoke `.chat.completions.create`
    directly, so the recorder owns the retry). Set `retry=False` when the
    caller already wraps the proxy in `create_with_retry` (e.g. the planner /
    capability router), so the gateway isn't retried twice over.
    """

    def __init__(self, client: OpenAI, retry: bool = True) -> None:
        self._client = client
        self._retry = retry
        self.calls: list[dict] = []
        self.purpose = "flow"
        self.chat = _RecChat(self)


class _RecChat:
    def __init__(self, rec: _RecordingLLM) -> None:
        self.completions = _RecCompletions(rec)


class _RecCompletions:
    def __init__(self, rec: _RecordingLLM) -> None:
        self._rec = rec

    def create(self, *args: Any, **kwargs: Any) -> Any:
        rec = self._rec
        started = time.monotonic()
        record: dict[str, Any] = {
            "ts": time.time(),
            "purpose": rec.purpose,
            "model": kwargs.get("model"),
            "messages": _sanitize_flow_messages(kwargs.get("messages", [])),
            "kwargs": {k: kwargs[k] for k in ("temperature", "max_tokens")
                       if k in kwargs},
        }
        try:
            # Retry transient gateway failures (timeout/5xx/rate-limit) before
            # giving up — one flaky call shouldn't sink the whole flow leg.
            # Skip when the caller already retries (rec._retry=False) so we
            # don't nest create_with_retry over itself.
            resp = (
                create_with_retry(rec._client, *args, **kwargs)
                if rec._retry
                else rec._client.chat.completions.create(*args, **kwargs)
            )
        except Exception as e:  # best-effort logging — record then re-raise
            record["elapsed_s"] = round(time.monotonic() - started, 3)
            record["response"] = None
            record["error"] = repr(e)
            rec.calls.append(record)
            raise
        record["elapsed_s"] = round(time.monotonic() - started, 3)
        msg = resp.choices[0].message if getattr(resp, "choices", None) else None
        # qwen can null `content` and put the answer in `reasoning_content`.
        record["response"] = (
            (getattr(msg, "content", None) or getattr(msg, "reasoning_content", None))
            if msg is not None else None
        )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            record["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        rec.calls.append(record)
        return resp


# --------------------------------------------------------------------------- #
# FlowRunner
# --------------------------------------------------------------------------- #


class FlowRunner:
    def __init__(
        self,
        flow_path: Path,
        env_overrides: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self.flow_path = flow_path
        self.flow = yaml.safe_load(flow_path.read_text(encoding="utf-8"))
        if "steps" not in self.flow:
            raise ValueError(f"Flow {flow_path} has no `steps`")

        self.env = ensure_llm_env(ENV_FILE, env_overrides)

        self.extra_args = extra_args or []
        # Wrapped so every flow-process LLM call (leg judge, bind extraction)
        # is recorded and later folded into each leg's traj.json — see
        # `_RecordingLLM` / `_fold_flow_llm_calls`.
        self._llm = _RecordingLLM(
            OpenAI(base_url=self.env["LLM_BASE_URL"], api_key=self.env["LLM_API_KEY"])
        )

        # Each flow run gets its own traj root, with one dir per leg
        # (`NN_<step-id>/`) holding that leg's trajectory directly — the
        # subprocess is pointed there via RELAY_TRAJ_DIR (see _run_app_step), so
        # there is no global `traj_logs/user_task/` scratch and no `user_task/`
        # subdir. Named with the timestamp first, then the apps it touches:
        # `<ts>_plan_<app1>_<app2>...`.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.flow_traj_root = REPO_ROOT / "traj_logs" / f"{ts}_{self._traj_stem()}"
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
                # A MobileWorld fallback leg with no app hint still gets a label
                # so an all-fallback flow isn't named after the file stem.
                if step.get("type") == MW_STEP_TYPE and "mw" not in apps:
                    apps.append("mw")
                continue
            short = str(pkg).rsplit(".", 1)[-1]
            if short and short not in apps:
                apps.append(short)
        return "plan_" + "_".join(apps) if apps else self.flow_path.stem

    # ------------------------------------------------------------------ run

    def run(self) -> dict[str, Any]:
        logger.info(f"FlowRunner start: {self.flow_path.name}  inputs={self.bb}")
        try:
            for step in self.flow["steps"]:
                kind = step.get("type") or "app_step"
                logger.info(f"--- step {step['id']!r} ({kind}) ---")
                if kind == "app_step":
                    self._run_app_step(step)
                elif kind == "ask_user":
                    self._run_ask_user(step)
                elif kind == MW_STEP_TYPE:
                    self._run_mobileworld_step(step)
                else:
                    raise ValueError(f"Unknown step type: {kind}")
                logger.info(f"blackboard after {step['id']!r}: {_redact(self.bb)}")
        finally:
            # Tear down a MobileWorld server WE started (no-op if none / reused).
            self._teardown_mw_server()
        logger.info("FlowRunner done")
        return self.bb

    # ------------------------------------------------------------ app_step

    def _run_app_step(self, step: dict) -> None:
        app = step["app"]
        capability = step["capability"]
        prompt = render(step["prompt"], self.bb)

        # Cold-launch is deferred to the agent's first predict
        # (RELAY_AGENT_LAUNCH below) so process/leg startup lands before the
        # launch and is excluded from the leg's task wall-clock (which the
        # agent writes to RELAY_WALL_OUT).
        self._step_idx += 1
        step_log_root = self.flow_traj_root / f"{self._step_idx:02d}_{step['id']}"
        step_log_root.mkdir(parents=True, exist_ok=True)
        # Mark where this leg's flow-process LLM calls (judge + extract) begin in
        # the recorder buffer so we can fold exactly this leg's slice below.
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
            # The native runner reads LLM_* + RELAY_* from the child env.
            cmd = [
                sys.executable, "-m", NATIVE_RUNNER_MODULE, app, prompt,
                *self.extra_args,
            ]
            logger.info(
                f"→ native runner for app={app} capability={capability!r} prompt={prompt!r}"
            )
            # Feed empty stdin so the final ask_user handoff (when present)
            # closes cleanly with EOF rather than blocking the flow.
            # The framework-excluded per-leg wall_clock.json is written by the
            # agent (RELAY_WALL_OUT) at subprocess exit; here we only print the
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
            # preserves it.
            timing = os.getenv("RELAY_TIMING", "0") == "1"
            t0 = time.monotonic()
            rc = subprocess.call(cmd, cwd=REPO_ROOT, env=child_env, stdin=subprocess.DEVNULL)
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
            if not reply and needs_reply:
                raise RuntimeError(
                    f"Step {step['id']!r}: no reply captured at {reply_path}. "
                    f"Check the sub-run's {step_log_root}/."
                )
            if not needs_reply:
                _assert_output_free_step_completed(step, summary, rc, summary_path)
            if reply:
                logger.info(f"captured reply ({len(reply)} chars) from {app}")
            else:
                logger.info(f"no reply captured for output-free step {step['id']!r}")
            # Semantic outcome check on top of the hard signals above: a leg can
            # reach a terminal state with a non-empty reply yet still not have
            # accomplished the goal. Best-effort — a judge failure must never
            # abort the flow (see leg_judge module docstring).
            self._judge_leg(
                step, app, capability, prompt, reply, step_log_root,
                summary.get("last_action_type"),
            )
        finally:
            try:
                reply_path.unlink()
            except OSError:
                pass

        # A falsy bind (missing, null, or "") means nothing downstream consumes
        # this leg — don't write it (a `bind: null` would otherwise land as a
        # None key in the blackboard). `_extract` itself makes an LLM call, so
        # keep it inside the window folded below.
        if step.get("bind"):
            if "extract" in step:
                value = self._extract(reply, step["extract"])
            else:
                value = reply
            self.bb[step["bind"]] = value

        # Fold this leg's flow-process LLM calls (leg judge + bind extraction)
        # into the leg's traj.json top level, alongside the in-app agent's
        # `["0"]["llm_calls"]`. Best-effort — a logging gap must not break the flow.
        self._fold_flow_llm_calls(step_log_root, llm_call_start)

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
        summary_path.write_text(
            json.dumps({"last_action_type": terminal_action,
                        "last_goal_status": goal_status,
                        "via": "mobileworld"}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        needs_reply = bool(step.get("bind") or step.get("extract"))
        if not reply and needs_reply:
            raise RuntimeError(
                f"MobileWorld leg {step['id']!r}: no answer captured. "
                f"Check {step_log_root}/user_task/."
            )
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
            data.setdefault("flow_llm_calls", []).extend(calls)
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
    ) -> None:
        """VLM success/failure check for a finished leg. Best-effort: logs the
        verdict and persists it next to the leg trajectory; never raises."""
        if os.getenv("RELAY_LEG_JUDGE", "1") != "1":
            return
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

            verdict = judge(frames)
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
        except Exception as e:  # judging is advisory — never break the flow
            logger.warning(f"leg judge errored for {step.get('id')!r}: {e}")

    # ---------------------------------------------------------- ask_user

    def _run_ask_user(self, step: dict) -> None:
        header = render(step.get("prompt_header", ""), self.bb)
        bind = step["bind"]

        if "select_from" in step:
            arr_key = step["select_from"]
            items = self.bb.get(arr_key) or []
            if not items:
                raise RuntimeError(f"ask_user {step['id']!r}: nothing in {arr_key!r} to choose from")
            label_tpl = step.get("item_label", "{name}")
            print(header)
            for i, it in enumerate(items, 1):
                print(f"  {i}. {render(label_tpl, it)}")
            print(f"  (1-{len(items)}, or empty to pick 1)", flush=True)
            try:
                raw = input("> ").strip()
            except EOFError:
                raw = ""
            chosen = _resolve_choice(raw, items, label_tpl)
            logger.info(f"user chose: {chosen}")
            self.bb[bind] = chosen
            return

        # plain freeform input
        print(header, flush=True)
        try:
            raw = input("> ").strip()
        except EOFError:
            raw = ""
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


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _parse_fenced_json(text: str) -> Any:
    m = _FENCE_RE.search(text)
    payload = m.group(1) if m else text
    # strict=False tolerates raw control characters (literal newlines/tabs) inside
    # string values, which some models emit instead of escaping them.
    return json.loads(payload, strict=False)


def _redact(d: dict[str, Any]) -> dict[str, Any]:
    """Shallow redact obvious secrets in blackboard logging."""
    out = {}
    for k, v in d.items():
        if "key" in k.lower() or "token" in k.lower():
            out[k] = "***"
        else:
            out[k] = v
    return out


def _load_mw_driver():
    """Load scripts/run_mobileworld.py as a module (server health/start/wait
    helpers live there; reuse rather than duplicate)."""
    path = REPO_ROOT / "scripts" / "run_mobileworld.py"
    spec = importlib.util.spec_from_file_location("run_mobileworld", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _harvest_mw_traj(leg_dir: Path) -> tuple[str, str | None, str | None]:
    """Pull (reply, terminal_action_type, goal_status) from a MobileWorld leg.

    MobileWorld's TrajLogger writes <leg_dir>/user_task/traj.json shaped like
    `{"0": {"traj": [{"action": {...}}, ...]}}`. The leg reply is the text of the
    last `answer` action; the terminal signal is the last action overall.
    Best-effort — a missing/garbled traj yields ("", None, None)."""
    traj = _read_json_file(leg_dir / "user_task" / "traj.json")
    node = traj.get("0") if isinstance(traj.get("0"), dict) else {}
    steps = node.get("traj") if isinstance(node.get("traj"), list) else []
    if not steps:
        return "", None, None
    last_action = (steps[-1].get("action") or {}) if isinstance(steps[-1], dict) else {}
    terminal_action = last_action.get("action_type")
    goal_status = last_action.get("goal_status")
    reply = ""
    for entry in reversed(steps):
        action = (entry.get("action") or {}) if isinstance(entry, dict) else {}
        if action.get("action_type") == ANSWER and (action.get("text") or "").strip():
            reply = action["text"].strip()
            break
    return reply, terminal_action, goal_status


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.exists() and path.stat().st_size > 0:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"failed to read native summary {path}: {exc}")
    return {}


def _assert_output_free_step_completed(
    step: dict,
    summary: dict[str, Any],
    rc: int,
    summary_path: Path,
) -> None:
    """No-reply legs still need a positive terminal signal from the child run."""
    last_action = summary.get("last_action_type")
    goal_status = summary.get("last_goal_status")
    ok = (
        rc == 0
        and (
            last_action in {ASK_USER, ANSWER}
            or (last_action == FINISHED and goal_status == "complete")
        )
    )
    if ok:
        return
    raise RuntimeError(
        f"Step {step['id']!r}: output-free native run did not reach a successful "
        f"terminal state (rc={rc}, last_action_type={last_action!r}, "
        f"last_goal_status={goal_status!r}). Check {summary_path.parent}."
    )


def _resolve_choice(raw: str, items: list[Any], label_tpl: str) -> Any:
    if not raw:
        return items[0]
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(items):
            return items[idx]
    # substring match against rendered label, then `name`
    lowered = raw.lower()
    for it in items:
        if lowered in render(label_tpl, it).lower():
            return it
    for it in items:
        if isinstance(it, dict) and lowered in str(it.get("name", "")).lower():
            return it
    raise ValueError(f"Could not resolve user choice {raw!r} among {len(items)} items")
