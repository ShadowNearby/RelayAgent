"""Native execution substrate for RelayAgent.

Drives the runtime loop through a `DeviceBackend` (Android: direct adb) in
an in-process `obs → predict → execute → obs` loop — no server, no
per-action HTTP round-trip.

- The agent (`agents/relay_agent.py`) does its own device I/O via the same
  backend / the `agents/_adb.py` shim. So the env object below exists only
  to serve the *runner loop* (initial screenshot + executing the actions
  predict returns).
- The action→gesture mapping covers swipe geometry, scroll-direction
  reversal, and the `skip_screenshot` last-frame reuse; concrete device
  commands (tap/swipe/keyboard) live in the backend.
- The agent is the sole writer of `wall_clock.json` (anchored at its first
  predict via `_begin_task_once`, written at process exit).

Device prerequisite done here: `setup_input_channel()` (Android: activate
the AdbKeyboard IME so the `ADB_INPUT_B64` broadcast is received).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from agents.device import DeviceBackend, Key, get_backend
from agents.runtime.interaction import get_interaction

from agents.agent.action_model import (
    ANSWER,
    ASK_USER,
    CLICK,
    DOUBLE_TAP,
    DRAG,
    ENV_FAIL,
    FINISHED,
    INPUT_TEXT,
    KEYBOARD_ENTER,
    LONG_PRESS,
    NAVIGATE_BACK,
    NAVIGATE_HOME,
    OPEN_APP,
    SCROLL,
    STATUS,
    SWIPE,
    UNKNOWN,
    WAIT,
    JSONAction,
)

_TERMINAL_TYPES = frozenset({FINISHED, UNKNOWN, ENV_FAIL})


@dataclass
class Observation:
    """Minimal observation carrier (only the fields the runner loop and the
    agent actually read)."""

    screenshot: Any
    ask_user_response: str | None = None


# ---------------------------------------------------------------------------
# Device prerequisite: input channel (Android: AdbKeyboard IME)
# ---------------------------------------------------------------------------
def activate_adb_keyboard() -> bool:
    """Legacy name — delegates to the default backend's input-channel setup
    (Android: enable + set the AdbKeyboard IME)."""
    return get_backend().setup_input_channel()


def reset_ime() -> None:
    """Legacy name — restore the device's default input state."""
    get_backend().teardown_input_channel()


# ---------------------------------------------------------------------------
# NativeEnv — direct-adb device interface for the runner loop
# ---------------------------------------------------------------------------
class NativeEnv:
    """The device interface the runner loop uses: get_observation /
    get_screenshot / execute_action, plus an empty `tools`."""

    def __init__(
        self, step_wait_time: float = 0.5, backend: DeviceBackend | None = None
    ) -> None:
        self.backend = backend or get_backend()
        self.step_wait_time = step_wait_time
        self.tools: list[dict] = []
        self._last_screenshot: Any = None
        # The WAIT action's sleep, read per-action (default 0.2s).
        self._wait_seconds = float(os.getenv("RELAY_WAIT_SECONDS", "0.2"))

    # -- observation -------------------------------------------------------
    def get_screenshot(self, wait_to_stabilize: bool = False):
        if wait_to_stabilize and self.step_wait_time > 0:
            # Settle detection returns as soon as the screen goes quiet
            # (scrcpy stream only; P2-S2); otherwise the fixed sleep as before.
            if not self.backend.wait_settled(self.step_wait_time):
                time.sleep(self.step_wait_time)
        img = None
        for attempt in range(3):
            img = self.backend.screencap()
            if img is not None:
                break
            logger.warning(f"screencap returned None (attempt {attempt + 1}/3); retrying")
            time.sleep(0.3)
        if img is None:
            # Last-frame fallback keeps the loop alive; predict reads .size for
            # coordinate math, so a stale frame of the right resolution is safe.
            if self._last_screenshot is not None:
                logger.warning("screencap failed; reusing last frame")
                return self._last_screenshot
            raise RuntimeError("screencap failed and no prior frame to reuse")
        self._last_screenshot = img
        return img

    def get_observation(self, type: str = "screenshot", wait_to_stabilize: bool = True) -> dict:
        return {"screenshot": self.get_screenshot(wait_to_stabilize=wait_to_stabilize)}

    # -- action execution (mirrors server.py:/step + controller.py) --------
    def execute_action(self, action: JSONAction) -> Observation:
        at = action.action_type
        try:
            self._dispatch(action)
        except Exception as e:  # never let one bad action kill the loop
            logger.warning(f"error executing {at}: {e}")

        # relay-patch: deterministic look-ahead steps tag skip_screenshot so we
        # reuse the last real frame instead of paying for a fresh screencap.
        skip = bool(
            (action.action_json or {}).get("skip_screenshot")
            and at != ASK_USER
            and self._last_screenshot is not None
        )
        if skip:
            shot = self._last_screenshot
        else:
            shot = self.get_screenshot(wait_to_stabilize=True)
        return Observation(screenshot=shot)

    def _dispatch(self, action: JSONAction) -> None:
        at = action.action_type
        if at == CLICK:
            self.backend.tap(int(action.x), int(action.y))
        elif at == SWIPE:
            self._swipe(action.x, action.y, action.direction or "up")
        elif at == INPUT_TEXT:
            text = action.text or ""
            if text != "":
                self.backend.input_text(text)
            else:
                logger.warning("input_text empty, skipping")
        elif at == NAVIGATE_BACK:
            self.backend.key(Key.BACK)
        elif at == NAVIGATE_HOME:
            self.backend.key(Key.HOME)
        elif at == KEYBOARD_ENTER:
            self.backend.key(Key.ENTER)
        elif at == LONG_PRESS:
            self.backend.long_press(int(action.x), int(action.y))
        elif at == DOUBLE_TAP:
            self.backend.double_tap(int(action.x), int(action.y))
        elif at == DRAG:
            self.backend.swipe_gesture(
                int(action.start_x), int(action.start_y),
                int(action.end_x), int(action.end_y),
            )
        elif at == SCROLL:
            # scroll maps to swipe with REVERSED vertical direction (scroll up =
            # content moves up = swipe down-ward visually).
            if action.direction in ("left", "right"):
                direction = action.direction
            else:
                direction = "down" if action.direction == "up" else "up"
            self._swipe(None, None, direction)
        elif at == OPEN_APP:
            self._open_app(action.app_name)
        elif at == WAIT:
            if not self.backend.wait_settled(self._wait_seconds):
                time.sleep(self._wait_seconds)
        elif at in (ANSWER, STATUS):
            pass  # device no-op; loop handles termination/answer text
        else:
            logger.warning(f"native env: unhandled action_type={at!r}")

    # -- direction → gesture geometry (platform-neutral) --------------------
    def _swipe(self, x: int | None, y: int | None, direction: str) -> None:
        w, h = self.backend.screen_size()
        if x is None:
            x = w // 2
        if y is None:
            y = h // 2
        unit = int(w / 10) * 2
        if direction == "up":
            dx, dy = 0, -2 * unit
        elif direction == "down":
            dx, dy = 0, 2 * unit
        elif direction == "left":
            dx, dy = -unit, 0
        elif direction == "right":
            dx, dy = unit, 0
        else:
            logger.warning(f"invalid swipe direction {direction!r}")
            return
        self.backend.swipe_gesture(x, y, x + dx, y + dy)

    def _open_app(self, app_name: str | None) -> None:
        # In the scripted single-app path open_app is skipped (the agent owns
        # cold-launch). This defensive branch only fires if a plan emits it: an
        # app id (has a dot) is launched; a bare label can't be resolved to an
        # app id here, so warn.
        if app_name and "." in app_name:
            self.backend.launch(app_name)
        else:
            logger.warning(f"open_app({app_name!r}): not an app id; cannot resolve label")


# ---------------------------------------------------------------------------
# StepLogger — per-step trajectory dump
# ---------------------------------------------------------------------------
# Position-bearing action types: the (x, y) / start..end the marker is drawn at.
_TAP_TYPES = frozenset({CLICK, DOUBLE_TAP, LONG_PRESS})
_VECTOR_TYPES = frozenset({SWIPE, SCROLL, DRAG})


class StepLogger:
    """Records every step's screenshot, action and click position to disk.

    **On by default.** Disable for performance benchmarking with
    ``RELAY_STEP_LOG=0`` (it writes — and for taps/swipes re-encodes — a PNG
    per step, a real per-step disk/CPU cost we don't want skewing timings).

    Layout, under ``<traj_dir>/steps/`` (``traj_dir`` defaults to the live
    ``traj_logs/user_task`` so it rides along with the backup rotation;
    override via ``RELAY_STEP_LOG_DIR``):

      - ``step_<n>.png``           the screenshot the agent acted on
      - ``step_<n>_marked.png``    same frame with the click dot / swipe arrow
                                   drawn on it (only when the action has coords)
      - ``steps.json``             index list: step, ts, action_type, the full
                                   action dict, the click ``(x, y)``, and the
                                   agent's thought/prediction string
    """

    def __init__(self, traj_dir: Path) -> None:
        self.dir = traj_dir / "steps"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "steps.json"
        self._records: list[dict] = []
        logger.info(f"step logging ON → {self.dir} (RELAY_STEP_LOG=0 to disable)")

    @classmethod
    def maybe_create(cls) -> "StepLogger | None":
        if os.getenv("RELAY_STEP_LOG", "1") == "0":
            logger.info("step logging OFF (RELAY_STEP_LOG=0)")
            return None
        # RELAY_STEP_LOG_DIR overrides the steps location outright; otherwise
        # ride along with the run's traj dir (RELAY_TRAJ_DIR, set per leg by the
        # flow runner) so steps/ lands next to traj.json. Default: global dir.
        override = os.getenv("RELAY_STEP_LOG_DIR") or os.getenv("RELAY_TRAJ_DIR")
        traj_dir = Path(override) if override else Path("traj_logs") / "user_task"
        try:
            return cls(traj_dir)
        except OSError as e:
            logger.warning(f"step logging disabled: cannot create dir: {e}")
            return None

    def record(self, step: int, screenshot: Any, action: JSONAction, prediction: Any) -> None:
        """Persist one step. Best-effort: never let logging kill the run."""
        try:
            self._record(step, screenshot, action, prediction)
        except Exception as e:  # logging must never break the loop
            logger.warning(f"step logging failed at step {step}: {e}")

    def _record(self, step: int, screenshot: Any, action: JSONAction, prediction: Any) -> None:
        at = action.action_type
        raw_name = f"step_{step:04d}.png"
        if screenshot is not None and hasattr(screenshot, "save"):
            screenshot.save(self.dir / raw_name)
        else:
            raw_name = None  # type: ignore[assignment]

        click = self._click_point(action)
        marked_name = None
        if screenshot is not None and hasattr(screenshot, "save") and (
            at in _TAP_TYPES or at in _VECTOR_TYPES
        ):
            marked = self._annotate(screenshot, action)
            if marked is not None:
                marked_name = f"step_{step:04d}_marked.png"
                marked.save(self.dir / marked_name)

        rec = {
            "step": step,
            "ts": round(time.time(), 3),
            "action_type": at,
            "action": action.model_dump(exclude_none=True),
            "click": click,
            "thought": str(prediction) if prediction is not None else None,
            "screenshot": raw_name,
            "marked_screenshot": marked_name,
        }
        self._records.append(rec)
        # Rewrite the whole index each step so a crashed run still leaves a
        # valid JSON file (steps are few; cost is negligible).
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(self._records, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _click_point(action: JSONAction) -> list[int] | None:
        if action.x is not None and action.y is not None:
            return [int(action.x), int(action.y)]
        return None

    @staticmethod
    def _annotate(screenshot: Any, action: JSONAction):
        """Return a copy of the frame with the action's position drawn on it:
        a red dot+crosshair for taps, a red arrow for swipe/scroll/drag."""
        from PIL import ImageDraw

        img = screenshot.convert("RGB")
        draw = ImageDraw.Draw(img)
        red = (255, 0, 0)
        at = action.action_type

        if at in _TAP_TYPES and action.x is not None and action.y is not None:
            x, y = int(action.x), int(action.y)
            r = 26
            draw.ellipse([x - r, y - r, x + r, y + r], outline=red, width=4)
            draw.line([x - r - 8, y, x + r + 8, y], fill=red, width=2)
            draw.line([x, y - r - 8, x, y + r + 8], fill=red, width=2)
            return img

        if at == DRAG and action.start_x is not None and action.end_x is not None:
            StepLogger._arrow(draw, action.start_x, action.start_y,
                              action.end_x, action.end_y, red)
            return img

        if at in (SWIPE, SCROLL):
            w, h = img.size
            cx = int(action.x) if action.x is not None else w // 2
            cy = int(action.y) if action.y is not None else h // 2
            # Draw the swipe direction; SCROLL maps to the inverse swipe (see
            # _dispatch), but for the marker we show the action's own direction.
            span = min(w, h) // 6
            d = (action.direction or "up").lower()
            dx, dy = {"up": (0, -span), "down": (0, span),
                      "left": (-span, 0), "right": (span, 0)}.get(d, (0, -span))
            StepLogger._arrow(draw, cx, cy, cx + dx, cy + dy, red)
            return img
        return None

    @staticmethod
    def _arrow(draw, x0, y0, x1, y1, color) -> None:
        import math

        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        draw.line([x0, y0, x1, y1], fill=color, width=5)
        ang = math.atan2(y1 - y0, x1 - x0)
        for off in (math.radians(150), math.radians(-150)):
            hx = x1 + 22 * math.cos(ang + off)
            hy = y1 + 22 * math.sin(ang + off)
            draw.line([x1, y1, int(hx), int(hy)], fill=color, width=5)


# ---------------------------------------------------------------------------
# Runner loop — replaces core/user_task_runner/runner.py:_execute_user_task
# ---------------------------------------------------------------------------
def run_task(goal: str, agent: Any, env: NativeEnv, max_step: int = -1) -> dict:
    """Drive `agent` to completion against `env`. Returns a small summary."""
    logger.info(f"native run_task: goal={goal!r} max_step={max_step}")
    agent.initialize(goal)

    step_log = StepLogger.maybe_create()
    obs = Observation(screenshot=env.get_observation()["screenshot"])
    step = 0
    last_action_type = UNKNOWN
    last_goal_status = None

    interaction = get_interaction()
    while True:
        if interaction.should_stop():
            logger.info("stop requested via interaction provider; ending task")
            break
        step += 1
        prediction, action = agent.predict(
            {
                "screenshot": obs.screenshot,
                "tool_call": None,
                "ask_user_response": obs.ask_user_response,
            }
        )
        if prediction is None:
            logger.warning(f"agent prediction failed at step {step}")
            break

        at = action.action_type
        last_action_type = at
        last_goal_status = action.goal_status
        logger.info(f"[step {step}] {at} :: {prediction}")
        interaction.emit_status(
            {"event": "step", "step": step, "action_type": at,
             "thought": str(prediction)}
        )

        # Log the frame the agent acted on + the action/click position. obs is
        # the pre-action screenshot, so the action's (x, y) reference it.
        if step_log is not None:
            step_log.record(step, obs.screenshot, action, prediction)

        if at in _TERMINAL_TYPES:
            break
        if at == ASK_USER:
            # Handoff endpoint. Under batch runs stdin is redirected; an EOF
            # (ask_user → None) is the documented SUCCESS terminal, not a
            # failure. On Android the overlay's take-over button maps to None.
            question = action.text or "The agent needs your input"
            user_response = interaction.ask_user(f"\n🤖 {question}")
            if user_response is None:
                logger.info("ask_user got EOF/take-over — handoff terminal, ending")
                break
            shot = env.get_screenshot(wait_to_stabilize=True)
            obs = Observation(screenshot=shot, ask_user_response=user_response)
        elif at == ANSWER:
            # Terminal: ANSWER is a device no-op and the loop ends here, so
            # skip execute_action's post-step screencap (~1.5s of dead time).
            break
        else:
            obs = env.execute_action(action)

        if max_step > 0 and step >= max_step:
            logger.info(f"max_step {max_step} reached")
            break

    agent.done()
    usage = agent.get_total_token_usage()
    logger.info(f"native run_task done: steps={step} last={last_action_type} tokens={usage}")
    return {
        "steps": step,
        "last_action_type": last_action_type,
        "last_goal_status": last_goal_status,
        "token_usage": usage,
    }
