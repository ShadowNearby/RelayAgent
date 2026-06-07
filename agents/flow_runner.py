"""Multi-app flow runner.

Executes a flow plan (the step/bind schema produced by `FlowPlanner` and
persisted under `manifests/_generated/` by `scripts/run_plan.py`) as a
sequence of (a) `run_native` sub-runs pinned to one app + one capability,
(b) user-input prompts, and (c) text-LLM extract steps that parse the
last sub-run's captured reply into structured data.

Design notes (see CLAUDE.md for project context):

- Each app step is a fresh `run_native` subprocess. We DON'T reuse one long-
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
import re
import shutil
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

from agents.runtime_config import ensure_llm_env

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
# Each app leg is a fresh `run_native.py` subprocess (direct adb).
RUN_NATIVE = REPO_ROOT / "scripts" / "run_native.py"


# cold-launch delegates to agents._adb so run_native/flow_runner/relay_agent
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
        self._llm = OpenAI(base_url=self.env["LLM_BASE_URL"], api_key=self.env["LLM_API_KEY"])

        # Each flow run gets its own traj root so the sub-runs don't keep
        # overwriting `traj_logs/user_task/`. The agent's TrajLogger always
        # writes to `<log_file_root>/user_task/`, so we give each step its own
        # `log_file_root` and group them under one flow-scoped parent named
        # after the apps it touches: `plan_<app1>_<app2>...`.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.flow_traj_root = REPO_ROOT / "traj_logs" / f"{self._traj_stem()}_{ts}"
        self._step_idx = 0
        logger.info(f"flow traj root: {self.flow_traj_root}")

        self.bb: dict[str, Any] = {}

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
                continue
            short = str(pkg).rsplit(".", 1)[-1]
            if short and short not in apps:
                apps.append(short)
        return "plan_" + "_".join(apps) if apps else self.flow_path.stem

    # ------------------------------------------------------------------ run

    def run(self) -> dict[str, Any]:
        logger.info(f"FlowRunner start: {self.flow_path.name}  inputs={self.bb}")
        for step in self.flow["steps"]:
            kind = step.get("type") or "app_step"
            logger.info(f"--- step {step['id']!r} ({kind}) ---")
            if kind == "app_step":
                self._run_app_step(step)
            elif kind == "ask_user":
                self._run_ask_user(step)
            else:
                raise ValueError(f"Unknown step type: {kind}")
            logger.info(f"blackboard after {step['id']!r}: {_redact(self.bb)}")
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

        with tempfile.NamedTemporaryFile(
            mode="w+", suffix=".json", prefix="relay_reply_", delete=False
        ) as fh:
            reply_path = Path(fh.name)
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
                # Agent writes the framework-excluded wall_clock.json straight
                # into this leg's dir. run_native still rotates and writes the
                # global traj_logs/user_task/ (traj.json + token logs + steps/),
                # which the NEXT leg's startup would rotate away — so after the
                # sub-run we copy that global dir into this leg's user_task/ to
                # preserve the per-leg trajectory (see the copy below). Create
                # the dir first so the agent's wall_clock.json has a home.
                "RELAY_WALL_OUT": str(step_log_root / "user_task" / "wall_clock.json"),
            }
            (step_log_root / "user_task").mkdir(parents=True, exist_ok=True)
            # run_native reads LLM_* + RELAY_* from the child env (no flags).
            cmd = [
                sys.executable, str(RUN_NATIVE), app, prompt,
                *self.extra_args,
            ]
            logger.info(
                f"→ run_native for app={app} capability={capability!r} prompt={prompt!r}"
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
                logger.warning(f"run_native exited rc={rc}; continuing if reply was captured")

            # Preserve this leg's trajectory before the next leg's run_native
            # startup rotates the global traj_logs/user_task/ away. Merge the
            # global dir (traj.json, token logs, steps/, agent_reply.json) into
            # this leg's user_task/ without clobbering the agent's wall_clock.json
            # already written there. Best-effort: a logging gap must not abort
            # the flow.
            global_traj = REPO_ROOT / "traj_logs" / "user_task"
            if global_traj.is_dir():
                try:
                    shutil.copytree(
                        global_traj,
                        step_log_root / "user_task",
                        dirs_exist_ok=True,
                    )
                except OSError as e:
                    logger.warning(f"failed to copy leg trajectory into {step_log_root}: {e}")

            reply = ""
            if reply_path.exists() and reply_path.stat().st_size > 0:
                payload = json.loads(reply_path.read_text(encoding="utf-8"))
                reply = (payload.get("reply") or "").strip()
            if not reply:
                raise RuntimeError(
                    f"Step {step['id']!r}: no reply captured at {reply_path}. "
                    f"Check the sub-run's {step_log_root}/user_task/."
                )
            logger.info(f"captured reply ({len(reply)} chars) from {app}")
        finally:
            try:
                reply_path.unlink()
            except OSError:
                pass

        if "bind" not in step:
            return
        if "extract" in step:
            value = self._extract(reply, step["extract"])
        else:
            value = reply
        self.bb[step["bind"]] = value

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
    return json.loads(payload)


def _redact(d: dict[str, Any]) -> dict[str, Any]:
    """Shallow redact obvious secrets in blackboard logging."""
    out = {}
    for k, v in d.items():
        if "key" in k.lower() or "token" in k.lower():
            out[k] = "***"
        else:
            out[k] = v
    return out


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
