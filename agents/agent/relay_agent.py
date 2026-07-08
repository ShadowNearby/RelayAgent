"""RelayAgent — the capability-driven in-app agent.

Run with:

    python -m agents.runtime.native_runner com.aliyun.tongyi "在通义里点一杯蜜雪冰城"

Design:
- Subclass MCPAgent (agents/agent_base.py) for the provider-agnostic openai
  client, token accounting, and the model_name plumbing.
- One LLM call per task picks a capability + invocation text from the card.
- The rest of the turns walk a deterministic plan: open_app, taps using
  card screen fractions, input_text, submit, optional post-result flow.
- Text-based selectors (input field focus, post-result labels) try
  `uiautomator dump` first (precise, free, robust to redraws); only fall
  back to a small VLM grounding call if the text is not in the a11y tree.
- `wait_for_reply` decides the reply is done purely from uiautomator
  text-hash stability (no VLM `done` judgement — see the note in
  `wait_for_reply`), on a WALL-CLOCK budget (`max(5×typical_latency, 60)`
  seconds), not a
  poll-count budget. The reply text is scraped from the a11y tree; a VLM
  only reads the frame verbatim when the scrape comes up empty.
- Honors `handoff_to_user_required`: emits ask_user before the irreversible CTA.
"""

from __future__ import annotations

import json
import os
import atexit
import sys
import time
from pathlib import Path
from typing import Any

# The file→agent loader loads this file via importlib.util.spec_from_file_location,
# so the package directory is NOT on sys.path automatically. Add the repo root
# so the sibling modules under `agents/` resolve as a package.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from loguru import logger

# Aliased to a leading-underscore name on purpose: the file→agent loader
# (agents.runtime.native_runner:_load_agent_class) collects every BaseAgent subclass in
# this module via inspect.getmembers (alphabetically sorted) and instantiates
# the first. If the base were exposed as "MCPAgent" it would sort before
# "RelayAgent" and the loader would try to instantiate the abstract base → "Can't
# instantiate abstract class MCPAgent". "_MCPAgentBase" sorts AFTER "RelayAgent"
# (ASCII '_' > 'R'), so RelayAgent is picked.
from agents.agent.agent_base import MCPAgent as _MCPAgentBase
from agents.runtime._img import pil_to_base64
from agents.agent.action_model import JSONAction

from agents.runtime._adb import force_stop, swipe_down
from agents.runtime._adb import cold_launch as _cold_launch
from agents.runtime._adb import screencap as _adb_screencap
from agents.agent.action_planner import Step, build_plan
from agents.routing.capability_router import route_capability
from agents.routing.card_loader import load_card_by_app_id

_TARGET_APP_ENV = "RELAY_TARGET_APP"
_MANIFESTS_ENV = "RELAY_MANIFESTS"
_FRESH_CONV_ENV = "RELAY_FRESH_CONV"  # set to "0" to disable
_SKIP_OPEN_APP_ENV = "RELAY_SKIP_OPEN_APP"  # set to "0" to force a plan-level open_app step
_REPLY_OUT_ENV = "RELAY_REPLY_OUT"  # path; if set, captured reply is dumped as JSON at handoff/done
_DISMISS_PERMS_ENV = "RELAY_DISMISS_PERMISSIONS"  # set to "0" to disable system permission popup auto-dismiss
# A/B benchmark gates (default on; "0" reverts to the pre-optimization path so
# baseline vs optimized token/time can be measured — see CLAUDE.md §6/§8).
_PRECHECK_ENV = "RELAY_PRECHECK"  # set to "0" to disable the wait_for_reply two-stage precheck (baseline = VLM-poll every tick)
_SCRAPE_ENV = "RELAY_SCRAPE"  # set to "0" to disable uiautomator reply-text scrape (baseline = VLM-only extraction)
# Fairness gate vs the MobileWorld baseline. The baseline (general_e2e) has no
# scroll-to-capture: once the on-screen reply looks settled it just reads the
# visible frame and `answer`s. RelayAgent's manifests opt into full-reply
# capture (x_capture_full_reply -> capture_full), which scrolls offscreen reply
# chunks into view and stitches them. For an apples-to-apples A/B we let the
# benchmark force that off: set to "0" and wait_for_reply stops at "screen
# stable" and returns the first visible frame's text, never entering the
# scrolling capture phase. Default on (full capture). See CLAUDE.md §6.
_CAPTURE_FULL_ENV = "RELAY_CAPTURE_FULL_REPLY"
# Manifest-isolation ablation (§8.9). When "1", the adapter loads NO card and
# drives the same *delegation skeleton* — fresh conversation, type the whole
# user request, wait, accept-defaults advance, hand off before the irreversible
# CTA — but every affordance (input box, send, proceed buttons, the CTA stop)
# is VLM-grounded at runtime instead of read from a card. Isolates the value of
# *delegation* from the value of the authored manifest (see _build_no_manifest_plan).
_NO_MANIFEST_ENV = "RELAY_NO_MANIFEST"

# Defer the target-app cold-launch to the agent's FIRST predict by default.
# Subprocess startup, module import and IME activation all happen before the
# first predict — so they land BEFORE the app launch, outside both the screen
# recording and the task wall-clock. Set RELAY_AGENT_LAUNCH=0 to fall back to a
# plan-level open_app step. If RELAY_SKIP_OPEN_APP=0 is set without an explicit
# RELAY_AGENT_LAUNCH, that also selects the plan-level open_app path. If
# RELAY_SKIP_OPEN_APP is unset, it follows the launch mode so the default
# agent-owned launch does not duplicate open_app.
_AGENT_LAUNCH_ENV = "RELAY_AGENT_LAUNCH"
# Directory for the screen recording. When set, the agent starts the recorder
# at the same first-predict moment as the launch (so the framework boot is not
# on tape) and stops/finalizes it at process exit via atexit.
_RECORD_DIR_ENV = "RELAY_RECORD_DIR"
# Where to write the framework-excluded task wall_clock.json. Defaults to the
# live traj dir (traj_logs/user_task). Batch runners set this per task so
# aggregate_metrics finds wall_clock.json next to that task's traj.json.
_WALL_OUT_ENV = "RELAY_WALL_OUT"

# Skip the runtime's post-step screencap for deterministic steps. RelayAgent's
# plan is deterministic and most steps never read the incoming screenshot; the
# ~0.85s screencap + ~0.2s settle after such a step is dead time. When on, the
# agent tags those actions with action_json["skip_screenshot"] so NativeEnv
# reuses the last real frame instead of re-capturing. Auto-enabled by
# `run_plan.py --record` (the screen video already covers the run). Default off.
# See CLAUDE.md "录屏模式跳过每步截图".
_SKIP_STEP_SCREENSHOT_ENV = "RELAY_SKIP_STEP_SCREENSHOT"
# Small settle the agent sleeps inside predict after emitting a screencap-skipped
# deterministic action, so a tap's short animation lands before the next action.
_BLIND_STEP_SLEEP = float(os.getenv("RELAY_BLIND_STEP_SLEEP", "0.15"))
# Step kinds whose materialization reads the incoming observation screenshot
# (hash precheck + VLM poll). Their predecessor must NOT skip the screencap, and
# the step itself must keep getting fresh frames. tap_text / nm_ground_tap try
# uiautomator first and self-capture a fresh frame on VLM fallback (see
# _materialize), so they are treated as deterministic for look-ahead purposes.
_VISION_STEP_KINDS = frozenset({"wait_for_reply"})

# Inter-tick sleep on a wait_for_reply precheck skip. Keeps the poll loop from
# busy-spinning while the reply streams, but stacks on top of the runtime's own
# per-step settle (step_wait_time + WAIT sleep), so a large value
# just inflates every poll tick. 0.3s is enough to avoid a tight spin while
# letting the next observe happen promptly. Tunable via RELAY_POLL_SKIP_SLEEP.
_POLL_SKIP_SLEEP = float(os.getenv("RELAY_POLL_SKIP_SLEEP", "0.3"))


def _settle_or_sleep(seconds: float) -> None:
    """Pacing sleep, upgraded to frame-arrival settle detection when the scrcpy
    capture stream is live (P2-S2): returns as soon as the screen has been quiet
    for one quiet window, worst case `seconds` — identical to the fixed sleep it
    replaces. Any backend without the signal falls back to time.sleep."""
    if seconds <= 0:
        return
    try:
        from agents.device.factory import get_backend

        handled = get_backend().wait_settled(seconds)
    except Exception:  # noqa: BLE001 — pacing must never raise into predict
        handled = False
    if not handled:
        time.sleep(seconds)

# Where this run's trajectory lands (see CLAUDE.md). Defaults to the shared
# global dir; the flow runner pins it per leg via RELAY_TRAJ_DIR so traj.json /
# agent_reply.json land straight in the leg's dir (no global user_task copy).
# We append every LLM call into traj.json at top-level under "0".llm_calls so
# the calls live alongside the per-step traj entries. The per-step traj writer
# rewrites the whole file each step but preserves unknown sibling keys, so the
# field survives across step writes.
_TRAJ_DIR = (
    Path(os.environ["RELAY_TRAJ_DIR"]) if os.getenv("RELAY_TRAJ_DIR")
    else _REPO_ROOT / "traj_logs" / "user_task"
)

# Grounding, reply-scrape, permission-popup and LLM-logging helpers split into
# sibling modules; imported here because RelayAgent uses each as a module global
# (and tests import some of them from this module — see test_a11y_migration).
from agents.agent.relay_grounding import (  # noqa: E402
    _FENCE_ANY,
    _GROUNDING_SYSTEM,
    _JSON_FENCE,
    _extract_xy,
    _ground_text_via_a11y,
)
from agents.agent.relay_reply import (  # noqa: E402
    _NM_ADVANCE_SYSTEM,
    _REPLY_WATCH_SYSTEM,
    _crop_cutoffs,  # noqa: F401 — re-exported (tests import it from this module)
    _dump_visible_text_hash,
    _extract_reply_text_from_dump,
    _hash_screenshot_region,
    _normalize_for_dedup,
    _stitch_chunks,
)
from agents.agent.relay_permissions import _maybe_dismiss_permission_popup  # noqa: E402
from agents.agent.relay_llm_log import (  # noqa: E402
    _llm_purpose_from_messages,
    _sanitize_kwargs_for_log,
    _sanitize_messages_for_log,
)


class RelayAgent(_MCPAgentBase):
    """Card-driven agent. The model only picks capabilities and grounds text
    selectors; deterministic tap coordinates come from screen fractions."""

    def __init__(
        self,
        model_name: str,
        llm_base_url: str,
        api_key: str = "empty",
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(tools=tools or [], **kwargs)
        self.model_name = model_name
        self.llm_base_url = llm_base_url
        self.api_key = api_key
        self.build_openai_client(self.llm_base_url, self.api_key)

        self.target_app: str | None = os.getenv(_TARGET_APP_ENV)
        self.manifests_dir = (
            Path(os.environ[_MANIFESTS_ENV])
            if os.getenv(_MANIFESTS_ENV)
            else None
        )
        self.card: dict | None = None
        self.plan: list[Step] = []
        self.cursor: int = 0
        self._planned: bool = False
        self._reply_polls: int = 0
        self._reply_precheck_skips: int = 0
        self._reply_precheck_skips_since_vlm: int = 0
        self._reply_dump_fail_streak: int = 0
        # Elapsed-second mark (relative to _reply_start_ts) before which we
        # stop hammering a failing dump and just skip; None = no cooldown.
        self._reply_dump_cooldown_until: float | None = None
        self._reply_last_shot_hash: str | None = None
        self._reply_last_dump_text_hash: str | None = None
        self._reply_text_stable_streak: int = 0
        self._reply_empty_scrape_streak: int = 0
        self._reply_start_ts: float | None = None
        self._wait_text_start_ts: float | None = None
        self._last_agent_reply: str | None = None
        self._last_input_text: str | None = None
        # Multi-screen capture state for replies that exceed one viewport
        # (e.g. 小红书 点点 returns long answers with stacked POI cards). See
        # the `capture_full` branch in wait_for_reply.
        self._capture_phase: str | None = None  # None | "scrolling"
        self._captured_chunks: list[str] = []
        self._capture_scrolls: int = 0
        self._capture_idle: int = 0
        self.fresh_conversation: bool = os.getenv(_FRESH_CONV_ENV, "1") != "0"
        agent_launch_env = os.getenv(_AGENT_LAUNCH_ENV)
        skip_open_app_env = os.getenv(_SKIP_OPEN_APP_ENV)
        if agent_launch_env is None and skip_open_app_env == "0":
            self.agent_launch = False
        else:
            self.agent_launch = (agent_launch_env if agent_launch_env is not None else "1") != "0"
        self.skip_open_app: bool = (
            skip_open_app_env == "1"
            if skip_open_app_env is not None
            else self.agent_launch
        )
        self.dismiss_permissions: bool = os.getenv(_DISMISS_PERMS_ENV, "1") != "0"
        self.precheck_enabled: bool = os.getenv(_PRECHECK_ENV, "1") != "0"
        self.scrape_enabled: bool = os.getenv(_SCRAPE_ENV, "1") != "0"
        self.capture_full_enabled: bool = os.getenv(_CAPTURE_FULL_ENV, "1") != "0"
        self.no_manifest: bool = os.getenv(_NO_MANIFEST_ENV, "0") == "1"
        self.skip_step_screenshot: bool = os.getenv(_SKIP_STEP_SCREENSHOT_ENV, "0") == "1"
        self._permission_dismissed_count: int = 0
        # Deferred-launch / recording / task-clock state. The app cold-launch,
        # the screen recorder, and the wall-clock anchor are all established on
        # the FIRST predict (see _begin_task_once) so subprocess/import/IME
        # startup lands before them and is excluded from both the recording
        # and wall_clock.json. _begin_task_once runs at most once per task.
        self.record_dir: str | None = os.getenv(_RECORD_DIR_ENV) or None
        self._task_started: bool = False
        self._task_t0: float | None = None
        self._recorder: Any = None
        # no-manifest ablation state (RELAY_NO_MANIFEST)
        self._nm_advance_iters: int = 0
        self._nm_ground_retries: dict[int, int] = {}

    def openai_chat_completions_create(  # type: ignore[override]
        self,
        model: str,
        messages: list[dict],
        **kwargs: Any,
    ) -> str | None:
        """Wrap MCPAgent's LLM call so every invocation is appended to
        traj.json. Image payloads are replaced with a short placeholder so the
        log stays human-readable; token deltas are computed from MCPAgent's
        running totals so we record per-call usage."""
        started = time.monotonic()
        pre_completion = self._total_completion_tokens
        pre_prompt = self._total_prompt_tokens
        pre_cached = self._total_cached_tokens
        purpose = _llm_purpose_from_messages(messages)
        try:
            raw = super().openai_chat_completions_create(
                model=model, messages=messages, **kwargs
            )
        except Exception as e:  # pragma: no cover — best-effort logging
            self._append_llm_call({
                "ts": time.time(),
                "elapsed_s": round(time.monotonic() - started, 3),
                "purpose": purpose,
                "model": model,
                "messages": _sanitize_messages_for_log(messages),
                "response": None,
                "error": repr(e),
                "plan_step": self.cursor if self._planned else None,
                "kwargs": _sanitize_kwargs_for_log(kwargs),
            })
            raise
        self._append_llm_call({
            "ts": time.time(),
            "elapsed_s": round(time.monotonic() - started, 3),
            "purpose": purpose,
            "model": model,
            "messages": _sanitize_messages_for_log(messages),
            "response": raw,
            "usage_delta": {
                "completion_tokens": self._total_completion_tokens - pre_completion,
                "prompt_tokens": self._total_prompt_tokens - pre_prompt,
                "cached_tokens": self._total_cached_tokens - pre_cached,
            },
            "plan_step": self.cursor if self._planned else None,
            "kwargs": _sanitize_kwargs_for_log(kwargs),
        })
        return raw

    def _append_llm_call(self, record: dict) -> None:
        """Append one LLM-call record to traj_logs/user_task/traj.json under
        log_data["0"]["llm_calls"]. Defensive: creates the bucket and stub
        traj/tools fields so the first traj write does not KeyError on them."""
        traj_path = _TRAJ_DIR / "traj.json"
        try:
            if not traj_path.exists():
                return
            try:
                with open(traj_path, encoding="utf-8") as f:
                    data = json.load(f)
            except json.JSONDecodeError:
                # Either a fresh `{}` mid-write or a corrupted file. Skip this
                # record rather than clobber the traj writer.
                return
            if not isinstance(data, dict):
                return
            bucket = data.setdefault("0", {})
            bucket.setdefault("tools", None)
            bucket.setdefault("traj", [])
            bucket.setdefault("llm_calls", []).append(record)
            with open(traj_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except OSError as e:
            logger.warning(f"Failed to append LLM call to traj.json: {e}")

    def initialize_hook(self, instruction: str) -> None:
        logger.info(f"RelayAgent init: instruction={instruction!r}")
        if not self.target_app:
            raise RuntimeError(
                f"{_TARGET_APP_ENV} must be set to the target app's package id "
                "(e.g. com.aliyun.tongyi)."
            )
        if self.no_manifest:
            # Manifest-isolation ablation: load NO card at all. The relay drives
            # the delegation skeleton with runtime VLM grounding only.
            self.card = None
            logger.info(
                f"{_NO_MANIFEST_ENV}=1: manifest-free delegation relay for "
                f"{self.target_app} (no card loaded)"
            )
        else:
            self.card = load_card_by_app_id(self.target_app, self.manifests_dir)
            logger.info(
                f"Loaded card {self.card['app_id']} v{self.card.get('card_version')} "
                f"({self.card['app_name']})"
            )
        self.plan = []
        self.cursor = 0
        self._planned = False

    def reset(self) -> None:
        self.card = None
        self.plan = []
        self.cursor = 0
        self._planned = False
        self._reply_polls = 0
        self._reply_precheck_skips = 0
        self._reply_precheck_skips_since_vlm = 0
        self._reply_dump_fail_streak = 0
        self._reply_dump_cooldown_until = None
        self._reply_last_shot_hash = None
        self._reply_last_dump_text_hash = None
        self._reply_text_stable_streak = 0
        self._reply_empty_scrape_streak = 0
        self._reply_start_ts = None
        self._wait_text_start_ts = None
        self._last_agent_reply = None
        self._last_input_text = None
        self._capture_phase = None
        self._captured_chunks = []
        self._capture_scrolls = 0
        self._capture_idle = 0
        self._permission_dismissed_count = 0
        self._nm_advance_iters = 0
        self._nm_ground_retries = {}

    def _build_no_manifest_plan(self) -> list[Step]:
        """Manifest-free delegation relay (§8.9 manifest-isolation ablation).

        Same *delegation skeleton* as the card path — open a fresh conversation,
        type the WHOLE user request as one turn, wait for the reply, accept the
        assistant's defaults and advance, then hand off before any irreversible
        CTA — but every affordance is VLM-grounded at runtime instead of read
        from a card. The only app fact used is the package id (which the runner
        already supplies via RELAY_TARGET_APP, exactly as general_e2e gets it).
        Isolates the value of delegation from the value of the authored manifest.
        """
        plan: list[Step] = []
        if not self.skip_open_app:
            plan.append(Step("open_app", {"package": self.target_app}, note="cold-launch"))
        if self.fresh_conversation:
            plan.append(Step(
                "nm_ground_tap",
                {"desc": "the control that starts a NEW / blank conversation "
                         "(a pencil or compose icon, a ＋, or a 新建对话 / 新对话 label)",
                 "ui_candidates": ["新建对话", "新对话", "New chat", "新建"],
                 "optional": True},
                note="fresh conversation (VLM-grounded)",
            ))
        plan.append(Step(
            "nm_ground_tap",
            {"desc": "the chat message input text box where you type a message to "
                     "the assistant (a wide field, usually along the bottom)",
             "optional": False},
            note="focus input (VLM-grounded)",
        ))
        plan.append(Step("input_text", {"text": self.instruction}, note="user query"))
        plan.append(Step(
            "nm_ground_tap",
            {"desc": "the SEND button for the message just typed (an up-arrow or "
                     "paper-plane icon, or a 发送 button, beside the input box)",
             "ui_candidates": ["发送", "Send"],
             "optional": False},
            note="submit (VLM-grounded)",
        ))
        plan.append(Step("wait_for_reply", {"max_seconds": 60}, note="await assistant reply"))
        plan.append(Step("nm_advance", {"max_iters": 6}, note="accept-defaults advance / CTA stop"))
        plan.append(Step("handoff", {"reason": "manifest-free relay reached the pre-CTA screen"}))
        return plan

    def _begin_task_once(self) -> None:
        """First-predict hook: anchor the task wall-clock, optionally launch the
        target app, and optionally start the screen recorder — in that order, so
        subprocess/import/IME startup (already done by the time predict is first
        called) is excluded from both the recording and wall_clock.json.
        Idempotent: the body runs once per task.

        atexit (fires when the native runner subprocess exits — on finished, on EOF
        after a handoff ask_user, or on error) finalizes the recording and
        writes wall_clock.json (phase="task"). The scripts no longer write
        wall_clock.json themselves; this is the source of truth.
        """
        if self._task_started:
            return
        self._task_started = True
        self._task_t0 = time.monotonic()

        if self.record_dir:
            try:
                from agents.runtime import _recorder
                self._recorder = _recorder.start(Path(self.record_dir))
                logger.info(f"screen recording (agent-owned) → {self.record_dir}")
            except Exception as e:  # recording must never abort the task
                logger.warning(f"recorder start failed: {e}")
                self._recorder = None

        if self.agent_launch and self.target_app:
            logger.info(f"agent-owned cold-launch of {self.target_app} (post-framework)")
            try:
                _cold_launch(self.target_app)
            except Exception as e:
                logger.warning(f"agent cold-launch failed: {e}")

        atexit.register(self._finalize_task)

    def _finalize_task(self) -> None:
        """Stop the recorder and write the framework-excluded task wall-clock.
        Registered with atexit; tolerant of being called once at process exit."""
        if self._recorder is not None:
            try:
                final = self._recorder.stop()
                if final:
                    logger.info(f"recording saved → {final}")
            except Exception as e:
                logger.warning(f"recorder stop failed: {e}")
            self._recorder = None
        if self._task_t0 is not None:
            wall_s = round(time.monotonic() - self._task_t0, 1)
            out = os.getenv(_WALL_OUT_ENV)
            wall_path = (
                Path(out) if out
                else _REPO_ROOT / "traj_logs" / "user_task" / "wall_clock.json"
            )
            try:
                if wall_path.parent.is_dir():
                    wall_path.write_text(
                        json.dumps({"wall_s": wall_s, "phase": "task"}),
                        encoding="utf-8",
                    )
                    logger.info(f"task wall_s={wall_s} (framework-excluded) → {wall_path}")
                else:
                    logger.warning(f"wall_clock dir missing: {wall_path.parent}")
            except OSError as e:
                logger.warning(f"wall_clock write failed: {e}")
            self._task_t0 = None

    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        self._begin_task_once()
        screenshot = observation["screenshot"]
        screen_w, screen_h = screenshot.size

        if not self._planned:
            if self.no_manifest:
                self.plan = self._build_no_manifest_plan()
                cap_id = "(no-manifest)"
            else:
                cap_id, invocation = route_capability(self, self.instruction, self.card)
                self.plan = build_plan(
                    self.card,
                    cap_id,
                    invocation,
                    fresh_conversation=self.fresh_conversation,
                    skip_open_app=self.skip_open_app,
                )
            self._planned = True
            logger.info(
                f"Plan ({len(self.plan)} steps) for capability={cap_id!r}: "
                + " → ".join(f"{s.kind}" for s in self.plan)
            )
            if self.skip_step_screenshot:
                logger.info(
                    "skip-step-screenshot ON: deterministic steps reuse last "
                    f"frame (settle={_BLIND_STEP_SLEEP}s); vision steps "
                    f"{sorted(_VISION_STEP_KINDS)} keep fresh capture"
                )

        # System permission popup hook — runs BEFORE the planned step. If a
        # known permission controller is foreground we tap the most-permissive
        # Allow, return a no-op wait that does NOT advance the cursor, and let
        # the runtime capture a fresh screenshot. Next predict re-enters cleanly
        # with the popup gone. Bounded so a stuck dialog can't infinite-loop.
        MAX_DISMISSALS = 8
        if (
            self.dismiss_permissions
            and self._permission_dismissed_count < MAX_DISMISSALS
        ):
            label = _maybe_dismiss_permission_popup()
            if label is not None:
                self._permission_dismissed_count += 1
                thought = (
                    f"system permission popup: tapped {label!r} "
                    f"(#{self._permission_dismissed_count}/{MAX_DISMISSALS})"
                )
                logger.info(thought)
                return thought, JSONAction(action_type="wait")

        if self.cursor >= len(self.plan):
            return ("plan exhausted", JSONAction(action_type="finished", goal_status="complete"))

        step_idx = self.cursor  # 0-based index of the step we're about to run
        step = self.plan[step_idx]
        action, advance, extra_note = self._materialize(step, screenshot, screen_w, screen_h)
        if advance:
            self.cursor += 1

        # Look-ahead screencap skip: if the NEXT step the runner will feed a
        # screenshot to is vision-independent, tell NativeEnv to skip its
        # post-step screencap and reuse the last real frame. The next
        # step is plan[cursor] after advancing, or the same held step otherwise;
        # a plan-exhausted next step (→ finished) needs no screenshot either.
        if self.skip_step_screenshot:
            nxt = self.plan[self.cursor] if self.cursor < len(self.plan) else None
            if nxt is None or nxt.kind not in _VISION_STEP_KINDS:
                action.action_json = {**(action.action_json or {}), "skip_screenshot": True}
                if _BLIND_STEP_SLEEP > 0:
                    _settle_or_sleep(_BLIND_STEP_SLEEP)

        note = step.note + (f"; {extra_note}" if extra_note else "")
        # Display 1-based index of the CURRENT step (the one we just emitted
        # an action for), not the next one. wait_for_reply re-enters the same
        # index until it advances, which is fine and visible in the suffix.
        suffix = "" if advance else " [hold]"
        thought = (
            f"step {step_idx + 1}/{len(self.plan)}: {step.kind} ({note}){suffix}"
        )
        logger.info(f"{thought} → {action.model_dump(exclude_none=True)}")
        return thought, action

    def _fresh_vision_frame(self, screenshot):
        """Return a frame safe for VLM grounding. In skip-step-screenshot mode
        the incoming `screenshot` may be a reused stale frame, so capture the
        current screen directly; fall back to the incoming one on capture
        failure or when skipping is off."""
        if not self.skip_step_screenshot:
            return screenshot
        fresh = _adb_screencap()
        if fresh is None:
            logger.warning("skip-mode VLM fallback: screencap failed; using stale frame")
            return screenshot
        return fresh

    def _materialize(
        self,
        step: Step,
        screenshot,
        screen_w: int,
        screen_h: int,
    ) -> tuple[JSONAction, bool, str]:
        """Return (action, advance_cursor, extra_note)."""
        kind = step.kind
        p = step.payload

        if kind == "open_app":
            # Cold-launch policy: always force-stop before launching so the
            # in-app agent observes a clean home surface. The native runner does
            # the FULL cold-launch (force-stop + monkey LAUNCHER) before reaching
            # this code path; duplicating force-stop here covers direct
            # invocations that bypass the runner-owned launch.
            pkg = p["package"]
            try:
                force_stop(pkg)
            except Exception as e:  # pragma: no cover — best-effort
                logger.warning(f"force-stop {pkg} failed (continuing): {e}")
            # The open_app action carries the launcher label (e.g. "千问"), not
            # the package id. Prefer the card's embedded_agent.name as the
            # launcher label; fall back to app_name, then the package id.
            launcher_label = (
                (self.card or {}).get("embedded_agent", {}).get("name")
                or (self.card or {}).get("app_name")
                or pkg
            )
            return JSONAction(action_type="open_app", app_name=launcher_label), True, ""

        if kind == "tap_fraction":
            return JSONAction(
                action_type="click",
                x=int(p["x_ratio"] * screen_w),
                y=int(p["y_ratio"] * screen_h),
            ), True, ""

        if kind == "tap_text":
            # 1. Try uiautomator XML first (precise, free, robust to UI redraws).
            #    Retry briefly to absorb animation latency (drawer open, etc.).
            # 2. Fall back to the VLM only if the text was not in the a11y tree.
            xy = None
            for attempt in range(3):
                xy = _ground_text_via_a11y(p["text"], screen_w, screen_h)
                if xy is not None:
                    break
                if attempt < 2:
                    time.sleep(0.8)
            if xy is not None:
                x, y = xy
                note = "uiautomator"
            else:
                frame = self._fresh_vision_frame(screenshot)
                x, y = self._ground_text(p["text"], frame, screen_w, screen_h)
                note = "VLM"
            return JSONAction(action_type="click", x=x, y=y), True, note

        if kind == "input_text":
            # Save the typed text so the reply-extraction heuristic in
            # wait_for_reply can use it to locate the user's own bubble in
            # the message list (everything visually BELOW is the reply).
            self._last_input_text = p["text"]
            return JSONAction(action_type="input_text", text=p["text"]), True, ""

        if kind == "nm_ground_tap":
            # Manifest-free affordance: VLM-ground a semantic description (no
            # card selector). Optional steps (e.g. fresh-conversation) skip on
            # miss; required ones retry a couple ticks before giving up.
            desc = p["desc"]
            optional = bool(p.get("optional"))
            # Manifest-free, but still app-agnostic: try generic affordance
            # vocabulary via uiautomator (e.g. "发送"/"新建对话") before paying
            # for a VLM grounding call. These are generic UI words, not a card
            # selector. Falls through to the VLM on miss.
            for cand in p.get("ui_candidates") or []:
                hit = _ground_text_via_a11y(cand, screen_w, screen_h)
                if hit is not None:
                    return JSONAction(action_type="click", x=hit[0], y=hit[1]), True, (
                        f"uiautomator {cand!r}"
                    )
            try:
                frame = self._fresh_vision_frame(screenshot)
                x, y = self._ground_text(desc, frame, screen_w, screen_h)
            except Exception as e:
                budget = self._nm_ground_retries.get(self.cursor, 0)
                if not optional and budget < 2:
                    self._nm_ground_retries[self.cursor] = budget + 1
                    logger.warning(
                        f"nm_ground_tap {desc!r} grounding failed ({e}); "
                        f"retry {budget + 1}/2"
                    )
                    return JSONAction(action_type="wait"), False, f"ground retry {budget + 1}/2"
                if not optional:
                    # A required affordance (input box / send button) could not
                    # be grounded after retries. Continuing would type into
                    # nothing and misreport success — end the run honestly.
                    logger.warning(
                        f"nm_ground_tap {desc!r} REQUIRED grounding exhausted: {e}; "
                        "finishing incomplete"
                    )
                    return (
                        JSONAction(action_type="finished", goal_status="incomplete"),
                        True,
                        "required grounding failed",
                    )
                logger.warning(f"nm_ground_tap {desc!r} skipped (optional): {e}")
                return JSONAction(action_type="wait"), True, "grounding failed; skipped"
            return JSONAction(action_type="click", x=x, y=y), True, "VLM-grounded"

        if kind == "wait_ms":
            return JSONAction(action_type="wait"), True, ""

        if kind == "wait_text":
            # Poll uiautomator until `text` shows up or timeout elapses. Each
            # call to _materialize is one tick of the runner's step loop;
            # we hold the cursor (advance=False) while waiting so subsequent
            # ticks re-enter this branch.
            target = p.get("text") or ""
            timeout_ms = int(p.get("timeout_ms", 5000))
            if not target:
                return JSONAction(action_type="wait"), True, "no text; bare wait"
            if self._wait_text_start_ts is None:
                self._wait_text_start_ts = time.monotonic()
            hit = _ground_text_via_a11y(target, screen_w, screen_h)
            elapsed_ms = int((time.monotonic() - self._wait_text_start_ts) * 1000)
            if hit is not None:
                logger.info(
                    f"wait_text: {target!r} appeared after {elapsed_ms}ms"
                )
                self._wait_text_start_ts = None
                return JSONAction(action_type="wait"), True, (
                    f"text {target!r} present ({elapsed_ms}ms)"
                )
            if elapsed_ms >= timeout_ms:
                logger.warning(
                    f"wait_text: {target!r} did not appear within "
                    f"{timeout_ms}ms; advancing anyway"
                )
                self._wait_text_start_ts = None
                return JSONAction(action_type="wait"), True, (
                    f"timeout after {elapsed_ms}ms"
                )
            return JSONAction(action_type="wait"), False, (
                f"waiting for {target!r} ({elapsed_ms}ms/{timeout_ms}ms)"
            )

        if kind == "wait_for_reply":
            # Fairness gate: RELAY_CAPTURE_FULL_REPLY=0 (baseline A/B) forces the
            # scroll-capture off so we stop at "screen stable" and return the
            # first visible frame's text — matching the MobileWorld baseline,
            # which only reads the on-screen reply with no scroll-to-capture.
            capture_full = bool(p.get("capture_full")) and self.capture_full_enabled
            max_capture_scrolls = int(p.get("max_capture_scrolls", 6))

            # Phase 2: after done, walk through the rest of the reply by
            # swiping the visible portion off so the next chunk slides into
            # view; capture each frame's reply text. Stops on max scrolls or
            # when two consecutive frames produce no new text.
            #
            # We prefer the uiautomator scrape over a VLM call per frame —
            # the scrape is free (no tokens) and returns the full visible
            # text verbatim. VLM is only used as a fallback when the scrape
            # finds nothing (e.g. WebView-rendered replies whose text isn't
            # in the a11y tree).
            if self._capture_phase == "scrolling":
                text = ""
                source = "vlm"
                if self.scrape_enabled:
                    text = _extract_reply_text_from_dump(
                        self._last_input_text, screen_h
                    )
                    source = "scrape"
                if not text:
                    text = self._poll_agent_reply(screenshot)
                    source = "vlm_fallback" if self.scrape_enabled else "vlm"
                # Substring dedup with normalization: a new VLM-extracted
                # frame often repeats text from a previous frame but with
                # tiny formatting drift (whitespace, punctuation, markdown
                # numbering style). Comparing on a normalized form (no
                # whitespace, no punctuation noise) catches those duplicates
                # while we keep the richer original text for storage. If the
                # new chunk strictly EXTENDS an existing one, replace in
                # place so we end up with the longest variant.
                novel = False
                if text:
                    n_text = _normalize_for_dedup(text)
                    norms = [_normalize_for_dedup(c) for c in self._captured_chunks]
                    contained = any(n_text and n_text in nc for nc in norms)
                    if not contained:
                        replaced = False
                        for i, nc in enumerate(norms):
                            if nc and nc in n_text:
                                # New chunk is a superset — keep the longer one.
                                self._captured_chunks[i] = text
                                replaced = True
                                break
                        if not replaced:
                            self._captured_chunks.append(text)
                        novel = True
                if novel:
                    self._capture_idle = 0
                    logger.info(
                        f"Capture scroll {self._capture_scrolls}: +chunk "
                        f"({len(text)} chars, via {source})"
                    )
                else:
                    self._capture_idle += 1
                stop = (
                    self._capture_scrolls >= max_capture_scrolls
                    or self._capture_idle >= 2
                )
                if stop:
                    # Chunks were captured top→bottom in reading order;
                    # stitch adjacent chunks at their suffix/prefix overlap
                    # so duplicated seam content collapses.
                    full = _stitch_chunks(list(self._captured_chunks))
                    self._last_agent_reply = full
                    logger.info(
                        f"Reply capture complete: {len(self._captured_chunks)} "
                        f"chunks, {len(full)} chars total"
                    )
                    self._capture_phase = None
                    self._captured_chunks = []
                    self._capture_scrolls = 0
                    self._capture_idle = 0
                    return JSONAction(action_type="wait"), True, "capture done"
                self._capture_scrolls += 1
                # Issue our own larger-than-default swipe (the built-in
                # scroll is fixed at ~0.4*width vertical, which means many
                # frames + many VLM calls). Then return a no-op so the runtime
                # just captures the next screenshot.
                swipe_down()
                return (
                    JSONAction(action_type="wait"),
                    False,
                    f"capture scroll {self._capture_scrolls}/{max_capture_scrolls}",
                )

            # Phase 1: decide when the in-app reply is COMPLETE, purely from
            # uiautomator text-hash stability. Budget is wall-clock seconds.
            #
            # NOTE(no-vlm-done): we used to ask the VLM for a `done` flag here.
            # With qwen as the judge that was unreliable — it kept returning
            # done=false on a stable, fully-rendered reply, so every long reply
            # rode to the timeout ceiling. We do NOT ask the VLM at all: once
            # the visible reply text stops changing for STABLE_DUMPS_FOR_DONE
            # consecutive dumps we treat it as done and scrape the full text
            # verbatim. The VLM (`_poll_agent_reply`) only READS text when the
            # scrape comes up empty; it never decides doneness.
            if self._reply_start_ts is None:
                self._reply_start_ts = time.monotonic()
            max_seconds = max(1, int(p.get("max_seconds", 30)))
            elapsed = time.monotonic() - self._reply_start_ts

            # 3 byte-identical dumps (pixel-stable + unchanged visible text) is
            # well past any inter-token streaming gap, so we don't truncate a
            # reply that briefly pauses mid-stream.
            STABLE_DUMPS_FOR_DONE = 3
            MAX_DUMP_FAILS = 2
            # After MAX_DUMP_FAILS consecutive dump failures we back off for
            # this many elapsed seconds instead of disabling dumping for the
            # whole wait: uiautomator dump is flaky while an app is still
            # compositing its reply, and those failures are usually transient.
            DUMP_RETRY_BACKOFF = 5.0
            # Some apps render the reply in a WebView / canvas that isn't in the
            # a11y tree: the text-hash stabilizes (on chrome) but the a11y
            # scrape stays empty. When that happens we fall back to the VLM to
            # read the frame verbatim (see the empty-scrape branch below), so a
            # WebView reply — or the RELAY_SCRAPE=0 VLM-only baseline — is still
            # captured instead of silently advancing empty-handed.
            # An animated element (spinner, autoplay media, blinking cursor)
            # flips the pixel hash forever; force a text dump after this many
            # pixel-changed skips so it can't starve the text check.
            MAX_SKIPS_BEFORE_FORCE = 5

            # Timeout safety net: text never stabilized (or dumps kept
            # failing). Scrape whatever is on screen and advance.
            if elapsed >= max_seconds:
                text = None
                if self.scrape_enabled:
                    text = _extract_reply_text_from_dump(
                        self._last_input_text, screen_h
                    )
                logger.warning(
                    f"In-app agent reply text did not stabilize within "
                    f"{max_seconds}s ({self._reply_polls} dump(s)); advancing "
                    f"anyway (last text={text!r})"
                )
                self._last_agent_reply = text
                self._reply_polls = 0
                self._reply_precheck_skips = 0
                self._reply_precheck_skips_since_vlm = 0
                self._reply_dump_fail_streak = 0
                self._reply_dump_cooldown_until = None
                self._reply_last_shot_hash = None
                self._reply_last_dump_text_hash = None
                self._reply_text_stable_streak = 0
                self._reply_empty_scrape_streak = 0
                self._reply_start_ts = None
                return JSONAction(action_type="wait"), True, "timeout"

            force_dump = (
                self._reply_precheck_skips_since_vlm >= MAX_SKIPS_BEFORE_FORCE
            )

            # Stage 1 — free pixel-hash pre-skip. While the reply streams the
            # pixels mutate, so we wait without paying for a dump. We do NOT
            # reset the text-stable streak here: a pixel flip with unchanged
            # text (e.g. a blinking cursor) must not block convergence — the
            # next dump compares hashes and tells the truth.
            if self.precheck_enabled and not force_dump:
                shot_hash = _hash_screenshot_region(screenshot)
                shot_changed = shot_hash != self._reply_last_shot_hash
                self._reply_last_shot_hash = shot_hash
                if shot_changed:
                    self._reply_precheck_skips += 1
                    self._reply_precheck_skips_since_vlm += 1
                    _settle_or_sleep(_POLL_SKIP_SLEEP)
                    return (
                        JSONAction(action_type="wait"),
                        False,
                        (
                            f"precheck skip #{self._reply_precheck_skips} "
                            f"(screen changed) @ {elapsed:.1f}s/{max_seconds}s"
                        ),
                    )

            # Circuit breaker (recoverable): after repeated dump failures we
            # back off rather than disabling dumping for the rest of the wait
            # — the failures are usually transient compositing hiccups. We
            # skip cheaply until the cooldown elapses, then fall through and
            # retry one dump; a single success below clears the streak and
            # resumes normal cadence.
            if self._reply_dump_cooldown_until is not None:
                if elapsed < self._reply_dump_cooldown_until:
                    self._reply_precheck_skips += 1
                    _settle_or_sleep(_POLL_SKIP_SLEEP)
                    return (
                        JSONAction(action_type="wait"),
                        False,
                        (
                            f"dump cooling down, retry @ "
                            f"{self._reply_dump_cooldown_until:.1f}s "
                            f"@ {elapsed:.1f}s/{max_seconds}s"
                        ),
                    )
                # Cooldown elapsed — retry a dump (the streak stays armed, so
                # if it fails again we just re-arm the backoff below).
                self._reply_dump_cooldown_until = None

            # Stage 2 — pixels stable (or forced): dump + hash the visible text.
            text_hash = _dump_visible_text_hash()
            self._reply_precheck_skips_since_vlm = 0
            if text_hash is None:
                self._reply_dump_fail_streak += 1
                self._reply_text_stable_streak = 0
                if self._reply_dump_fail_streak >= MAX_DUMP_FAILS:
                    self._reply_dump_cooldown_until = (
                        elapsed + DUMP_RETRY_BACKOFF
                    )
                    logger.warning(
                        "wait_for_reply text-hash dump backing off "
                        f"{DUMP_RETRY_BACKOFF:.0f}s — "
                        f"{self._reply_dump_fail_streak} consecutive dump "
                        "failures; will retry, not give up"
                    )
                _settle_or_sleep(_POLL_SKIP_SLEEP)
                return (
                    JSONAction(action_type="wait"),
                    False,
                    f"dump failed @ {elapsed:.1f}s/{max_seconds}s",
                )

            self._reply_dump_fail_streak = 0
            self._reply_polls += 1
            prev = self._reply_last_dump_text_hash
            self._reply_last_dump_text_hash = text_hash
            if prev is not None and text_hash == prev:
                self._reply_text_stable_streak += 1
            else:
                # First dump, or text changed since the last dump (still
                # growing) — restart the stability count. New text also means
                # the screen is alive, so reset the empty-scrape giveup count.
                self._reply_text_stable_streak = 1
                self._reply_empty_scrape_streak = 0

            if self._reply_text_stable_streak < STABLE_DUMPS_FOR_DONE:
                _settle_or_sleep(_POLL_SKIP_SLEEP)
                return (
                    JSONAction(action_type="wait"),
                    False,
                    (
                        f"text stable {self._reply_text_stable_streak}/"
                        f"{STABLE_DUMPS_FOR_DONE} @ {elapsed:.1f}s/{max_seconds}s "
                        f"(+{self._reply_precheck_skips} precheck skips)"
                    ),
                )

            # Text has been byte-identical for STABLE_DUMPS_FOR_DONE dumps →
            # reply is complete. Scrape the full visible text verbatim.
            text = None
            text_source = "scrape"
            if self.scrape_enabled:
                text = _extract_reply_text_from_dump(
                    self._last_input_text, screen_h
                )
            if not text:
                # Stable screen but the a11y scrape came up empty. Two cases
                # land here and look identical from uiautomator: (a) the reply
                # hasn't rendered yet, (b) it lives in a non-a11y WebView/canvas
                # we can't scrape — and RELAY_SCRAPE=0 (the VLM-only baseline)
                # reaches here on EVERY reply by construction. Fall back to the
                # VLM to read the frame verbatim. Doneness was already decided
                # by text-hash stability above (see NOTE(no-vlm-done)); the VLM
                # only reads text off the screen.
                vlm_text = self._poll_agent_reply(screenshot)
                if vlm_text:
                    text = vlm_text
                    text_source = "vlm_fallback" if self.scrape_enabled else "vlm"
            if not text:
                # Neither the a11y scrape nor the VLM could read anything yet.
                # Don't report success — keep polling. The reply may still be
                # appearing (case a); if it truly never becomes readable the
                # timeout branch above makes the honest empty-handed call once
                # the ceiling is hit, so an answered task never silently
                # advances with no captured reply.
                self._reply_empty_scrape_streak += 1
                self._reply_text_stable_streak = 0
                _settle_or_sleep(_POLL_SKIP_SLEEP)
                return (
                    JSONAction(action_type="wait"),
                    False,
                    (
                        f"text stable but empty scrape+vlm "
                        f"(round {self._reply_empty_scrape_streak}) @ "
                        f"{elapsed:.1f}s/{max_seconds}s; waiting out ceiling"
                    ),
                )

            logger.info(
                f"In-app agent reply DONE (text stable {STABLE_DUMPS_FOR_DONE} "
                f"dumps, source={text_source}) after {self._reply_polls} "
                f"dump(s) / {self._reply_precheck_skips} precheck skip(s) / "
                f"{elapsed:.1f}s; text={text!r}"
            )
            self._last_agent_reply = text
            self._reply_polls = 0
            self._reply_precheck_skips = 0
            self._reply_precheck_skips_since_vlm = 0
            self._reply_dump_fail_streak = 0
            self._reply_dump_cooldown_until = None
            self._reply_last_shot_hash = None
            self._reply_last_dump_text_hash = None
            self._reply_text_stable_streak = 0
            self._reply_empty_scrape_streak = 0
            self._reply_start_ts = None
            if capture_full:
                self._capture_phase = "scrolling"
                self._captured_chunks = [text]
                self._capture_scrolls = 0
                self._capture_idle = 0
                swipe_down()
                return (
                    JSONAction(action_type="wait"),
                    False,
                    "done; entering full-reply capture",
                )
            return JSONAction(action_type="wait"), True, f"done; text={text!r}"

        if kind == "tap_unless_present":
            # Probe via uiautomator only (cheap + precise); fall through to
            # tap target if probe is missing. We deliberately do NOT fall
            # back to VLM for the probe — a VLM hallucination here would
            # cause a destructive tap on a non-idempotent UI toggle.
            probe = p["probe"]
            target = p["target"]
            probe_text = probe.get("text") or probe.get("text_contains")
            if probe_text and _ground_text_via_a11y(
                probe_text, screen_w, screen_h
            ) is not None:
                return JSONAction(action_type="wait"), True, (
                    f"probe {probe_text!r} present; skipping conditional tap"
                )
            # Probe missing → tap target. Keep this deterministic by using a
            # manifest-provided point, not VLM grounding.
            if "screen_fraction" in target:
                f = target["screen_fraction"]
                x = int(f["x_ratio"] * screen_w)
                y = int(f["y_ratio"] * screen_h)
            else:
                logger.warning(
                    f"tap_unless_present: unsupported target {target!r}; "
                    "screen_fraction is required. Skipping."
                )
                return JSONAction(action_type="wait"), True, "unsupported target"
            return JSONAction(action_type="click", x=x, y=y), True, (
                f"probe {probe_text!r} absent; tapping target"
            )

        if kind == "swipe":
            return JSONAction(action_type="scroll", direction=p.get("direction", "down")), True, ""

        if kind == "copy_reply":
            # Single-shot: tap the in-app 复制 button. We don't read the
            # clipboard back — Android's Binder 1MB cap rejects WeChat AI 搜索
            # copies (they include cited cards + HTML). The answer is left on
            # the device clipboard for the user / a downstream IME helper.
            #
            # Locator priority:
            #   1. VLM grounding via `text`
            #   2. Sanity-check the (x,y) against `valid_x` / `valid_y`. The
            #      copy icon is in a fixed COLUMN on this device — only the
            #      toolbar's y drifts with reply length — so a wildly off x
            #      is almost always a model miss. Snap x to the spec center
            #      (screen-fraction point) when VLM y is valid but x isn't.
            #   3. Hard fallback: screen-fraction point.
            spec_x = spec_y = None
            if p.get("screen_fraction"):
                f = p["screen_fraction"]
                spec_x = int(f["x_ratio"] * screen_w)
                spec_y = int(f["y_ratio"] * screen_h)
            vx = vy = None
            if p.get("text"):
                try:
                    frame = self._fresh_vision_frame(screenshot)
                    vx, vy = self._ground_text(p["text"], frame, screen_w, screen_h)
                    logger.info(
                        f"copy_reply: VLM-grounded {p['text']!r} -> ({vx},{vy})"
                    )
                except RuntimeError as e:
                    logger.warning(f"copy_reply: VLM grounding failed: {e}")

            def _in(rng, v):
                return rng is None or (rng[0] <= v <= rng[1])

            vx_ok = vx is not None and _in(p.get("valid_x"), vx)
            vy_ok = vy is not None and _in(p.get("valid_y"), vy)
            if vx_ok and vy_ok:
                x, y = vx, vy
                note = f"VLM ({vx},{vy})"
            elif vy_ok and spec_x is not None:
                # Common Qwen-VL failure mode: correct y, bogus x. Keep y.
                x, y = spec_x, vy
                logger.warning(
                    f"copy_reply: VLM x={vx} outside valid_x={p.get('valid_x')}; "
                    f"snapping x to spec_x={spec_x} (kept VLM y={vy})"
                )
                note = f"VLM-y + spec-x ({spec_x},{vy})"
            elif spec_x is not None and spec_y is not None:
                x, y = spec_x, spec_y
                logger.warning(
                    f"copy_reply: VLM unusable (vx={vx}, vy={vy}); using spec "
                    f"point ({spec_x},{spec_y})"
                )
                note = f"spec-point ({spec_x},{spec_y})"
            else:
                logger.warning("copy_reply: no usable locator; skipping tap")
                return JSONAction(action_type="wait"), True, "no copy locator"
            return (
                JSONAction(action_type="click", x=x, y=y),
                True,
                f"tap copy via {note}",
            )

        if kind == "nm_advance":
            # Manifest-free advance loop: VLM decides whether to (a) STOP at an
            # irreversible CTA → hand off, (b) tap a proceed/accept-defaults
            # button and re-enter, or (c) finish (informational reply / nothing
            # actionable). Conservative: unsure → stop. Holds the cursor while
            # advancing (like wait_for_reply).
            max_iters = int(p.get("max_iters", 6))
            # Refresh the scraped reply so the eventual handoff carries content.
            if self.scrape_enabled:
                try:
                    scraped = _extract_reply_text_from_dump(self._last_input_text, screen_h)
                    if scraped and (
                        not self._last_agent_reply
                        or len(scraped) > len(self._last_agent_reply)
                    ):
                        self._last_agent_reply = scraped
                except Exception:
                    pass
            probe = self._nm_probe_advance(screenshot)
            if probe.get("cta_present"):
                logger.info(
                    f"nm_advance: irreversible CTA {probe.get('cta_label')!r} "
                    "detected; stopping for handoff"
                )
                return JSONAction(action_type="wait"), True, (
                    f"CTA {probe.get('cta_label')!r} → handoff"
                )
            adv = probe.get("advance_xy")
            if adv and self._nm_advance_iters < max_iters:
                self._nm_advance_iters += 1
                ax, ay = adv
                logger.info(
                    f"nm_advance iter {self._nm_advance_iters}/{max_iters}: "
                    f"tap {probe.get('advance_label')!r} @ ({ax},{ay})"
                )
                return JSONAction(action_type="click", x=ax, y=ay), False, (
                    f"advance {self._nm_advance_iters}/{max_iters} "
                    f"{probe.get('advance_label')!r}"
                )
            reason = (
                "iters exhausted" if self._nm_advance_iters >= max_iters
                else "no further actionable step"
            )
            logger.info(f"nm_advance: {reason}; advancing to handoff")
            return JSONAction(action_type="wait"), True, reason

        if kind == "handoff":
            self._maybe_persist_reply()
            # Today this emits a terminal ask_user. In non-interactive batch runs,
            # stdin=EOF ends the subprocess after the reply has been persisted.
            # An interactive caller can answer and continue in the same task.
            reply_note = (
                f"\n\nAgent reply captured:\n{self._last_agent_reply}"
                if self._last_agent_reply
                else ""
            )
            return JSONAction(
                action_type="ask_user",
                text=(
                    f"Handing control back: {p.get('reason', '')}. The in-app "
                    "agent has surfaced the result; please review and confirm "
                    "any irreversible action yourself."
                    f"{reply_note}"
                ),
            ), True, ""

        if kind == "done":
            self._maybe_persist_reply()
            return JSONAction(action_type="finished", goal_status=p.get("status", "complete")), True, ""

        logger.warning(f"Unsupported step kind={kind}; emitting ask_user")
        return JSONAction(
            action_type="ask_user",
            text=f"Card step not supported by adapter: {kind} {p}",
        ), True, ""

    def _ground_text(
        self,
        target: str,
        screenshot,
        screen_w: int,
        screen_h: int,
    ) -> tuple[int, int]:
        b64 = pil_to_base64(screenshot)
        messages = [
            {"role": "system", "content": _GROUNDING_SYSTEM},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Click on the UI element matching: {target!r}",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            },
        ]
        raw = self.openai_chat_completions_create(
            model=self.model_name,
            messages=messages,
            temperature=0.0,
            max_tokens=128,
        )
        if not raw:
            raise RuntimeError(f"Grounding LLM returned empty for {target!r}")

        rx, ry = _extract_xy(raw)
        logger.info(
            f"Grounding {target!r} on {screen_w}x{screen_h}: raw={raw!r} "
            f"-> extracted=({rx},{ry})"
        )
        if rx is None or ry is None:
            raise RuntimeError(f"Grounding model could not find {target!r}")
        # Detect coordinate-system: if either value clearly exceeds the 0-999
        # normalized range, treat as absolute pixels in the source image.
        if rx > 999 or ry > 999:
            px, py = int(rx), int(ry)
        else:
            px, py = int(rx * screen_w / 999), int(ry * screen_h / 999)
        logger.info(f"Grounding {target!r}: mapped to pixel ({px},{py})")
        return px, py

    def _maybe_persist_reply(self) -> None:
        """Dump the captured in-app agent reply as JSON to:
          1. RELAY_REPLY_OUT (if set) — for parent batch/NL runners;
          2. <traj dir>/agent_reply.json — always, so the reply lives next
             to traj.json / screenshots (the flow runner pins <traj dir> per
             leg via RELAY_TRAJ_DIR). Best-effort; never raises."""
        payload = json.dumps(
            {
                "reply": self._last_agent_reply,
                "target_app": self.target_app,
            },
            ensure_ascii=False,
        )
        targets: list[Path] = []
        env_path = os.getenv(_REPLY_OUT_ENV)
        if env_path:
            targets.append(Path(env_path))
        # Drop the reply in the run's traj dir too so it's discoverable by
        # default (the flow runner pins this per leg via RELAY_TRAJ_DIR).
        if _TRAJ_DIR.exists():
            targets.append(_TRAJ_DIR / "agent_reply.json")
        for path in targets:
            try:
                path.write_text(payload, encoding="utf-8")
                logger.info(
                    f"Persisted captured reply to {path} "
                    f"({len(self._last_agent_reply or '')} chars)"
                )
            except OSError as e:
                logger.warning(f"Failed to persist reply to {path}: {e}")

    def _poll_agent_reply(self, screenshot) -> str | None:
        """Ask the VLM to read the in-app assistant's reply text off the
        screenshot. Doneness is decided elsewhere (text-hash stability, see
        NOTE(no-vlm-done)); this is a text reader only."""
        b64 = pil_to_base64(screenshot)
        messages = [
            {"role": "system", "content": _REPLY_WATCH_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Read the in-app assistant's reply text off this screen."},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            },
        ]
        raw = self.openai_chat_completions_create(
            model=self.model_name,
            messages=messages,
            temperature=0.0,
            # qwen is a thinking model: a complex reply screen burns the answer
            # budget on reasoning and returns null content. 600 was too tight
            # and timed the whole poll loop out; 2000 leaves room to finish.
            max_tokens=2000,
        )
        if not raw:
            logger.warning("Reply-watch LLM returned empty; no text read")
            return None

        m = _JSON_FENCE.search(raw)
        payload = m.group(1) if m else raw
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            import ast

            try:
                data = ast.literal_eval(payload)
            except (ValueError, SyntaxError):
                logger.warning(f"Reply-watch unparseable response: {raw!r}")
                return None
        if not isinstance(data, dict):
            return None
        text = data.get("text")
        if isinstance(text, str):
            text = text.strip() or None
        else:
            text = None
        return text

    def _nm_probe_advance(self, screenshot) -> dict:
        """VLM probe for the manifest-free advance loop. Returns
        {cta_present, cta_label, advance_xy: [px,py]|None, advance_label, done}.
        Conservative: any empty/unparseable response → cta_present=True (stop),
        so we never blindly tap toward an irreversible action."""
        out = {
            "cta_present": False, "cta_label": None,
            "advance_xy": None, "advance_label": None, "done": False,
        }
        b64 = pil_to_base64(screenshot)
        messages = [
            {"role": "system", "content": _NM_ADVANCE_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": "What is the next safe step? Follow the policy."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]},
        ]
        raw = self.openai_chat_completions_create(
            model=self.model_name, messages=messages, temperature=0.0, max_tokens=200,
        )
        if not raw:
            logger.warning("nm_advance probe empty; stopping for safety")
            out["cta_present"] = True
            out["cta_label"] = "(probe empty → stop)"
            return out
        m = _JSON_FENCE.search(raw) or _FENCE_ANY.search(raw)
        payload = m.group(1) if m else raw
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            import ast
            try:
                data = ast.literal_eval(payload)
            except (ValueError, SyntaxError):
                logger.warning(f"nm_advance probe unparseable: {raw!r}; stopping for safety")
                out["cta_present"] = True
                out["cta_label"] = "(unparseable → stop)"
                return out
        if not isinstance(data, dict):
            out["cta_present"] = True
            return out
        out["cta_present"] = bool(data.get("cta_present"))
        out["cta_label"] = data.get("cta_label")
        out["done"] = bool(data.get("done"))
        out["advance_label"] = data.get("advance_label")
        adv = data.get("advance")
        if isinstance(adv, list) and len(adv) == 2 and not out["cta_present"]:
            try:
                rx, ry = float(adv[0]), float(adv[1])
                if rx > 999 or ry > 999:
                    out["advance_xy"] = [int(rx), int(ry)]
                else:
                    w, h = screenshot.size
                    out["advance_xy"] = [int(rx * w / 999), int(ry * h / 999)]
            except (ValueError, TypeError):
                out["advance_xy"] = None
        return out
