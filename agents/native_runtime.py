"""Native (mw-free) execution substrate for RelayAgent.

This replaces the two pieces of MobileWorld we actually depend on for the
runtime loop — the FastAPI **server** (`core/server.py` + `runtime/controller.py`,
which turns a `JSONAction` into an `adb shell ...` over HTTP) and the **runner**
(`core/user_task_runner/runner.py`, the `obs → predict → execute → obs` loop) —
with direct `adb` calls and an in-process loop.

What it deliberately keeps identical to the mw path (so an A/B isolates exactly
the substrate, not the agent):

- The *same* `agents/relay_agent.py` is driven, unchanged. The agent never
  touched `self.env` — it already does all device I/O via `agents/_adb.py` and
  raw `uiautomator`/`subprocess`. So the env object below exists only to serve
  the *runner loop* (initial screenshot + executing the actions predict returns).
- `JSONAction` and the action-type constants are still imported from mw, and the
  action→adb mapping mirrors `server.py:/step` + `controller.py` byte-for-byte
  (swipe geometry, scroll-direction reversal, `ADB_INPUT_B64` keyboard broadcast,
  the relay-patch `skip_screenshot` last-frame reuse).
- The agent stays the sole writer of `wall_clock.json` (anchored at its first
  predict via `_begin_task_once`, written at process exit), so task wall-clock is
  measured the same way on both paths.

What it sheds vs the mw path: the uvicorn server, the per-action HTTP round-trip,
the screenshot file-write→b64→HTTP→decode detour (we pipe `exec-out screencap`
straight to PIL), and mw's framework cold-start.

Device prerequisite that mw's `prerequisite.py` used to do for us and we now do
here: activate the AdbKeyboard IME so `ADB_INPUT_B64` is received.
"""
from __future__ import annotations

import base64
import os
import subprocess
import time
from dataclasses import dataclass
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
    """Minimal carrier mirroring mw's runtime Observation (only the fields the
    runner loop and the agent actually read)."""

    screenshot: Any
    ask_user_response: str | None = None


# ---------------------------------------------------------------------------
# Device prerequisite: AdbKeyboard IME (mw's prerequisite.py did this for us)
# ---------------------------------------------------------------------------
def _adb(args: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        adb_base() + args, check=False, capture_output=True, text=True, timeout=timeout
    )


def activate_adb_keyboard() -> bool:
    """Enable + set AdbKeyboard as the active IME so input_text's
    `am broadcast -a ADB_INPUT_B64` is received. Returns True on success.

    Mirrors prerequisite._activate_adb_keyboard. We do NOT install it here —
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
    """Restore the device's default IME (mirrors prerequisite._deactivate)."""
    _adb(["shell", "ime", "reset"])


# ---------------------------------------------------------------------------
# NativeEnv — direct-adb replacement for AndroidEnvClient + server + controller
# ---------------------------------------------------------------------------
class NativeEnv:
    """Implements the slice of mw's AndroidEnvClient that the runner loop uses:
    get_observation / get_screenshot / execute_action, plus an empty `tools`."""

    def __init__(self, step_wait_time: float = 0.2) -> None:
        self.step_wait_time = step_wait_time
        self.tools: list[dict] = []
        self._last_screenshot: Any = None
        # MW_WAIT_SECONDS is the server-side WAIT sleep (relay-patch default 0.2).
        # On the native path it's just a local sleep, read per-action.
        self._wait_seconds = float(os.getenv("MW_WAIT_SECONDS", "0.2"))

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
            # mw maps scroll→swipe and REVERSES vertical direction (scroll up =
            # content moves up = swipe down-ward visually). Replicate exactly.
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

    # -- adb primitives (mirror controller.py) -----------------------------
    def _tap(self, x: int, y: int) -> None:
        _adb(["shell", "input", "tap", str(x), str(y)])

    def _keyevent(self, code: str) -> None:
        _adb(["shell", "input", "keyevent", code])

    def _input_text(self, text: str) -> None:
        # AdbKeyboard ADB_INPUT_B64 broadcast. Clean base64 (no surrounding
        # b'...' quotes — mw builds the quoted form for a shell string; we pass
        # an argv list so the keyboard receives the same decoded bytes).
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
        # package id (has a dot) is monkey-launched; a label can't be resolved
        # without mw's APP_DICT, so warn.
        if app_name and "." in app_name:
            _adb(["shell", "monkey", "-p", app_name, "-c",
                  "android.intent.category.LAUNCHER", "1"])
        else:
            logger.warning(f"open_app({app_name!r}): not a package id; cannot resolve label")


# ---------------------------------------------------------------------------
# Runner loop — replaces core/user_task_runner/runner.py:_execute_user_task
# ---------------------------------------------------------------------------
def run_task(goal: str, agent: Any, env: NativeEnv, max_step: int = -1) -> dict:
    """Drive `agent` to completion against `env`. Returns a small summary."""
    logger.info(f"native run_task: goal={goal!r} max_step={max_step}")
    agent.initialize(goal)

    obs = Observation(screenshot=env.get_observation()["screenshot"])
    step = 0
    last_action_type = UNKNOWN

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
        logger.info(f"[step {step}] {at} :: {prediction}")

        if at in _TERMINAL_TYPES:
            break
        if at == ASK_USER:
            # Handoff endpoint. Under a batch/flow the stdin is redirected; an
            # EOF here is the documented SUCCESS terminal, not a failure.
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
    return {"steps": step, "last_action_type": last_action_type, "token_usage": usage}
