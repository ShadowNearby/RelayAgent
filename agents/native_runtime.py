"""Native execution substrate for RelayAgent.

Drives the runtime loop with direct `adb` calls and an in-process
`obs → predict → execute → obs` loop — no server, no per-action HTTP
round-trip.

- The agent (`agents/relay_agent.py`) does all device I/O via
  `agents/_adb.py` and raw `uiautomator`/`subprocess`. So the env object
  below exists only to serve the *runner loop* (initial screenshot +
  executing the actions predict returns).
- The action→adb mapping covers swipe geometry, scroll-direction reversal,
  the `ADB_INPUT_B64` keyboard broadcast, and the `skip_screenshot`
  last-frame reuse.
- The agent is the sole writer of `wall_clock.json` (anchored at its first
  predict via `_begin_task_once`, written at process exit).
- Screenshots pipe `exec-out screencap` straight to PIL.

Device prerequisite done here: activate the AdbKeyboard IME so
`ADB_INPUT_B64` is received.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from agents._adb import adb_base, screencap
from agents._adb import _get_screen_size  # type: ignore[attr-defined]

from agents.action_model import (
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

_ADB_KEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"
_TERMINAL_TYPES = frozenset({FINISHED, UNKNOWN, ENV_FAIL})


@dataclass
class Observation:
    """Minimal observation carrier (only the fields the runner loop and the
    agent actually read)."""

    screenshot: Any
    ask_user_response: str | None = None


# ---------------------------------------------------------------------------
# Device prerequisite: AdbKeyboard IME
# ---------------------------------------------------------------------------
def _adb(args: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        adb_base() + args, check=False, capture_output=True, text=True, timeout=timeout
    )


def activate_adb_keyboard() -> bool:
    """Enable + set AdbKeyboard as the active IME so input_text's
    `am broadcast -a ADB_INPUT_B64` is received. Returns True on success.

    We do NOT install it here —
    the device already has com.android.adbkeyboard (verified); if it's missing,
    surface that loudly rather than silently degrading (per the
    surface-fallback-failures rule)."""
    installed = _adb(["shell", "pm", "list", "packages", "com.android.adbkeyboard"])
    if "com.android.adbkeyboard" not in (installed.stdout or ""):
        logger.warning(
            "AdbKeyboard (com.android.adbkeyboard) is NOT installed; input_text "
            "via ADB_INPUT_B64 will not work. Install ADBKeyboard.apk first."
        )
        return False
    _adb(["shell", "ime", "enable", _ADB_KEYBOARD_IME])
    res = _adb(["shell", "ime", "set", _ADB_KEYBOARD_IME])
    active = _adb(["shell", "settings", "get", "secure", "default_input_method"])
    ok = _ADB_KEYBOARD_IME.split("/")[0] in (active.stdout or "")
    if ok:
        logger.info(f"AdbKeyboard IME active: {(active.stdout or '').strip()}")
    else:
        logger.warning(
            f"ime set rc={res.returncode}; active IME still "
            f"{(active.stdout or '').strip()!r}"
        )
    return ok


def reset_ime() -> None:
    """Restore the device's default IME."""
    _adb(["shell", "ime", "reset"])


# ---------------------------------------------------------------------------
# NativeEnv — direct-adb device interface for the runner loop
# ---------------------------------------------------------------------------
class NativeEnv:
    """The device interface the runner loop uses: get_observation /
    get_screenshot / execute_action, plus an empty `tools`."""

    def __init__(self, step_wait_time: float = 0.5) -> None:
        self.step_wait_time = step_wait_time
        self.tools: list[dict] = []
        self._last_screenshot: Any = None
        # The WAIT action's sleep, read per-action (default 0.2s).
        self._wait_seconds = float(os.getenv("RELAY_WAIT_SECONDS", "0.2"))

    # -- observation -------------------------------------------------------
    def get_screenshot(self, wait_to_stabilize: bool = False):
        if wait_to_stabilize and self.step_wait_time > 0:
            time.sleep(self.step_wait_time)
        img = None
        for attempt in range(3):
            img = screencap()
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
        except subprocess.TimeoutExpired:
            logger.warning(f"adb timed out executing {at}")
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
            self._tap(int(action.x), int(action.y))
        elif at == SWIPE:
            self._swipe(action.x, action.y, action.direction or "up")
        elif at == INPUT_TEXT:
            text = action.text or ""
            if text != "":
                self._input_text(text)
            else:
                logger.warning("input_text empty, skipping")
        elif at == NAVIGATE_BACK:
            self._keyevent("KEYCODE_BACK")
        elif at == NAVIGATE_HOME:
            self._keyevent("KEYCODE_HOME")
        elif at == KEYBOARD_ENTER:
            self._keyevent("KEYCODE_ENTER")
        elif at == LONG_PRESS:
            x, y = int(action.x), int(action.y)
            _adb(["shell", "input", "swipe", str(x), str(y), str(x), str(y), "1000"])
        elif at == DOUBLE_TAP:
            self._tap(int(action.x), int(action.y))
            self._tap(int(action.x), int(action.y))
        elif at == DRAG:
            _adb(["shell", "input", "swipe", str(int(action.start_x)), str(int(action.start_y)),
                  str(int(action.end_x)), str(int(action.end_y)), "400"])
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
            time.sleep(self._wait_seconds)
        elif at in (ANSWER, STATUS):
            pass  # device no-op; loop handles termination/answer text
        else:
            logger.warning(f"native env: unhandled action_type={at!r}")

    # -- adb primitives ----------------------------------------------------
    def _tap(self, x: int, y: int) -> None:
        _adb(["shell", "input", "tap", str(x), str(y)])

    def _keyevent(self, code: str) -> None:
        _adb(["shell", "input", "keyevent", code])

    def _input_text(self, text: str) -> None:
        # AdbKeyboard ADB_INPUT_B64 broadcast. Clean base64 (no surrounding
        # b'...' quotes — we pass an argv list so the keyboard receives the
        # decoded bytes directly).
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        _adb(["shell", "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64])

    def _swipe(self, x: int | None, y: int | None, direction: str) -> None:
        w, h = _get_screen_size()
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
        _adb(["shell", "input", "swipe", str(x), str(y), str(x + dx), str(y + dy), "400"])

    def _open_app(self, app_name: str | None) -> None:
        # In the scripted single-app path open_app is skipped (the agent owns
        # cold-launch). This defensive branch only fires if a plan emits it: a
        # package id (has a dot) is monkey-launched; a bare label can't be
        # resolved to a package here, so warn.
        if app_name and "." in app_name:
            _adb(["shell", "monkey", "-p", app_name, "-c",
                  "android.intent.category.LAUNCHER", "1"])
        else:
            logger.warning(f"open_app({app_name!r}): not a package id; cannot resolve label")


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
        override = os.getenv("RELAY_STEP_LOG_DIR")
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

    while True:
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

        # Log the frame the agent acted on + the action/click position. obs is
        # the pre-action screenshot, so the action's (x, y) reference it.
        if step_log is not None:
            step_log.record(step, obs.screenshot, action, prediction)

        if at in _TERMINAL_TYPES:
            break
        if at == ASK_USER:
            # Handoff endpoint. Under batch runs stdin is redirected; an EOF
            # here is the documented SUCCESS terminal, not a failure.
            question = action.text or "The agent needs your input"
            try:
                user_response = input(f"\n🤖 {question}\n> ")
            except EOFError:
                logger.info("ask_user got EOF (stdin redirected) — handoff terminal, ending")
                break
            shot = env.get_screenshot(wait_to_stabilize=True)
            obs = Observation(screenshot=shot, ask_user_response=user_response)
        elif at == ANSWER:
            obs = env.execute_action(action)
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
