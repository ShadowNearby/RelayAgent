"""Device-less tests for NativeEnv's action→gesture mapping and the shared
trajectory-dir default.

Pins the CLAUDE.md-documented semantics that previously had zero coverage:
scroll up/down direction REVERSAL (left/right pass through), swipe geometry
(unit = int(w/10)*2 from the screen center), the skip_screenshot last-frame
reuse, the WAIT action's RELAY_WAIT_SECONDS sleep with the wait_settled
fallback seam, and the repo-anchored (never CWD-relative) default traj dir
shared by the runner and StepLogger.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents.agent.action_model import (
    ANSWER,
    ASK_USER,
    CLICK,
    DOUBLE_TAP,
    DRAG,
    INPUT_TEXT,
    KEYBOARD_ENTER,
    LONG_PRESS,
    NAVIGATE_BACK,
    NAVIGATE_HOME,
    OPEN_APP,
    SCROLL,
    STATUS,
    SWIPE,
    WAIT,
    JSONAction,
)
from agents.device.base import Key
from agents.runtime import native_runtime
from agents.runtime.native_runtime import NativeEnv, StepLogger, default_traj_dir


class FakeBackend:
    """Duck-typed DeviceBackend recording every call. Screen is 1000x2000, so
    the swipe unit is int(1000/10)*2 = 200 and the default origin (screen
    center) is (500, 1000)."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.settled = False
        self.screencap_count = 0

    def screen_size(self, timeout: float = 5.0):
        return (1000, 2000)

    def screencap(self, timeout: float = 5.0):
        self.screencap_count += 1
        return object()  # fresh frame sentinel per call

    def wait_settled(self, budget, quiet=None):
        self.calls.append(("wait_settled", budget))
        return self.settled

    def tap(self, x, y, **kw):
        self.calls.append(("tap", x, y))
        return True

    def long_press(self, x, y, **kw):
        self.calls.append(("long_press", x, y))

    def double_tap(self, x, y, **kw):
        self.calls.append(("double_tap", x, y))

    def swipe_gesture(self, x0, y0, x1, y1, **kw):
        self.calls.append(("swipe_gesture", x0, y0, x1, y1))

    def key(self, key):
        self.calls.append(("key", key))

    def input_text(self, text):
        self.calls.append(("input_text", text))

    def launch(self, app_id, **kw):
        self.calls.append(("launch", app_id))


def _swipes(backend: FakeBackend) -> list[tuple]:
    return [c for c in backend.calls if c[0] == "swipe_gesture"]


class DispatchGestureTests(unittest.TestCase):
    """The scroll↔swipe direction semantics CLAUDE.md dedicates a section to:
    a sign flip here reverses every manifest scroll and capture_full walk."""

    def setUp(self):
        self.backend = FakeBackend()
        self.env = NativeEnv(step_wait_time=0, backend=self.backend)

    def test_scroll_up_becomes_downward_swipe(self):
        # scroll up = content moves up = downward-target swipe gesture.
        self.env._dispatch(JSONAction(action_type=SCROLL, direction="up"))
        self.assertEqual(_swipes(self.backend), [("swipe_gesture", 500, 1000, 500, 1400)])

    def test_scroll_down_becomes_upward_swipe(self):
        self.env._dispatch(JSONAction(action_type=SCROLL, direction="down"))
        self.assertEqual(_swipes(self.backend), [("swipe_gesture", 500, 1000, 500, 600)])

    def test_scroll_left_right_not_reversed(self):
        self.env._dispatch(JSONAction(action_type=SCROLL, direction="left"))
        self.env._dispatch(JSONAction(action_type=SCROLL, direction="right"))
        self.assertEqual(
            _swipes(self.backend),
            [("swipe_gesture", 500, 1000, 300, 1000),
             ("swipe_gesture", 500, 1000, 700, 1000)],
        )

    def test_swipe_keeps_its_own_direction(self):
        # SWIPE (unlike SCROLL) is never reversed.
        self.env._dispatch(JSONAction(action_type=SWIPE, direction="up"))
        self.env._dispatch(JSONAction(action_type=SWIPE, direction="down"))
        self.assertEqual(
            _swipes(self.backend),
            [("swipe_gesture", 500, 1000, 500, 600),
             ("swipe_gesture", 500, 1000, 500, 1400)],
        )

    def test_swipe_uses_explicit_origin(self):
        self.env._dispatch(JSONAction(action_type=SWIPE, x=100, y=300, direction="right"))
        self.assertEqual(_swipes(self.backend), [("swipe_gesture", 100, 300, 300, 300)])

    def test_swipe_missing_direction_defaults_up(self):
        self.env._dispatch(JSONAction(action_type=SWIPE))
        self.assertEqual(_swipes(self.backend), [("swipe_gesture", 500, 1000, 500, 600)])

    def test_invalid_direction_no_gesture(self):
        # Unreachable via JSONAction (its validator rejects bad directions);
        # pin the guard in _swipe itself.
        self.env._swipe(None, None, "diagonal")
        self.assertEqual(_swipes(self.backend), [])


class DispatchActionTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.env = NativeEnv(step_wait_time=0, backend=self.backend)

    def test_click_taps(self):
        self.env._dispatch(JSONAction(action_type=CLICK, x=10, y=20))
        self.assertEqual(self.backend.calls, [("tap", 10, 20)])

    def test_key_mapping(self):
        self.env._dispatch(JSONAction(action_type=NAVIGATE_BACK))
        self.env._dispatch(JSONAction(action_type=NAVIGATE_HOME))
        self.env._dispatch(JSONAction(action_type=KEYBOARD_ENTER))
        self.assertEqual(
            self.backend.calls,
            [("key", Key.BACK), ("key", Key.HOME), ("key", Key.ENTER)],
        )

    def test_long_press_and_double_tap(self):
        self.env._dispatch(JSONAction(action_type=LONG_PRESS, x=1, y=2))
        self.env._dispatch(JSONAction(action_type=DOUBLE_TAP, x=3, y=4))
        self.assertEqual(
            self.backend.calls, [("long_press", 1, 2), ("double_tap", 3, 4)]
        )

    def test_drag_passes_endpoints(self):
        self.env._dispatch(
            JSONAction(action_type=DRAG, start_x=1, start_y=2, end_x=3, end_y=4)
        )
        self.assertEqual(self.backend.calls, [("swipe_gesture", 1, 2, 3, 4)])

    def test_input_text_empty_is_skipped(self):
        self.env._dispatch(JSONAction(action_type=INPUT_TEXT, text=""))
        self.assertEqual(self.backend.calls, [])
        self.env._dispatch(JSONAction(action_type=INPUT_TEXT, text="hi"))
        self.assertEqual(self.backend.calls, [("input_text", "hi")])

    def test_open_app_needs_an_app_id_not_a_label(self):
        self.env._dispatch(JSONAction(action_type=OPEN_APP, app_name="com.x.y"))
        self.assertEqual(self.backend.calls, [("launch", "com.x.y")])
        self.backend.calls.clear()
        self.env._dispatch(JSONAction(action_type=OPEN_APP, app_name="千问"))
        self.assertEqual(self.backend.calls, [])  # bare label: warn, no launch

    def test_answer_and_status_are_device_noops(self):
        self.env._dispatch(JSONAction(action_type=ANSWER))
        self.env._dispatch(JSONAction(action_type=STATUS))
        self.assertEqual(self.backend.calls, [])


class WaitActionTests(unittest.TestCase):
    def test_wait_sleeps_relay_wait_seconds_without_settle_signal(self):
        backend = FakeBackend()  # wait_settled → False (no signal)
        with mock.patch.dict(os.environ, {"RELAY_WAIT_SECONDS": "0.05"}):
            env = NativeEnv(step_wait_time=0, backend=backend)
        with mock.patch.object(native_runtime.time, "sleep") as sleep:
            env._dispatch(JSONAction(action_type=WAIT))
        self.assertIn(("wait_settled", 0.05), backend.calls)
        sleep.assert_called_once_with(0.05)

    def test_wait_skips_sleep_when_settled(self):
        backend = FakeBackend()
        backend.settled = True
        with mock.patch.dict(os.environ, {"RELAY_WAIT_SECONDS": "0.05"}):
            env = NativeEnv(step_wait_time=0, backend=backend)
        with mock.patch.object(native_runtime.time, "sleep") as sleep:
            env._dispatch(JSONAction(action_type=WAIT))
        sleep.assert_not_called()


class SkipScreenshotTests(unittest.TestCase):
    """The RELAY_SKIP_STEP_SCREENSHOT fast path: deterministic look-ahead
    steps tag skip_screenshot and must reuse the last frame instead of
    paying a fresh screencap."""

    def setUp(self):
        self.backend = FakeBackend()
        self.env = NativeEnv(step_wait_time=0, backend=self.backend)

    def _skip_action(self, action_type=CLICK, **kw) -> JSONAction:
        return JSONAction(
            action_type=action_type, action_json={"skip_screenshot": True}, **kw
        )

    def test_skip_reuses_last_frame(self):
        last = object()
        self.env._last_screenshot = last
        obs = self.env.execute_action(self._skip_action(x=1, y=2))
        self.assertIs(obs.screenshot, last)
        self.assertEqual(self.backend.screencap_count, 0)

    def test_ask_user_never_skips(self):
        old = object()
        self.env._last_screenshot = old
        obs = self.env.execute_action(self._skip_action(action_type=ASK_USER))
        self.assertEqual(self.backend.screencap_count, 1)
        self.assertIsNot(obs.screenshot, old)

    def test_no_prior_frame_takes_fresh(self):
        obs = self.env.execute_action(self._skip_action(x=1, y=2))
        self.assertEqual(self.backend.screencap_count, 1)
        self.assertIsNotNone(obs.screenshot)

    def test_without_flag_takes_fresh_and_updates_last(self):
        old = object()
        self.env._last_screenshot = old
        obs = self.env.execute_action(JSONAction(action_type=CLICK, x=1, y=2))
        self.assertEqual(self.backend.screencap_count, 1)
        self.assertIsNot(obs.screenshot, old)
        self.assertIs(self.env._last_screenshot, obs.screenshot)


class TrajDirDefaultTests(unittest.TestCase):
    """Every traj writer must resolve the same default dir — repo-anchored /
    RELAY_TRAJ_ROOT-aware, never relative to the invocation CWD (which used
    to split steps/ away from traj.json when run outside the repo)."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)  # patch.dict restores popped keys too
        for key in ("RELAY_TRAJ_ROOT", "RELAY_TRAJ_DIR",
                    "RELAY_STEP_LOG_DIR", "RELAY_STEP_LOG"):
            os.environ.pop(key, None)

    def test_default_is_repo_anchored_not_cwd(self):
        repo_root = Path(native_runtime.__file__).resolve().parent.parent.parent
        d = default_traj_dir()
        self.assertTrue(d.is_absolute())
        self.assertEqual(d, repo_root / "traj_logs" / "user_task")

    def test_relay_traj_root_redirects(self):
        with mock.patch.dict(os.environ, {"RELAY_TRAJ_ROOT": "/redirected/base"}):
            self.assertEqual(default_traj_dir(), Path("/redirected/base/user_task"))

    def test_matches_native_runner_resolution(self):
        from agents.runtime.native_runner import _resolve_traj_dir

        d, pinned = _resolve_traj_dir()
        self.assertFalse(pinned)
        self.assertEqual(d, default_traj_dir())

    def test_steplogger_default_is_cwd_independent(self):
        with tempfile.TemporaryDirectory() as root, \
             tempfile.TemporaryDirectory() as cwd, \
             mock.patch.dict(os.environ, {"RELAY_TRAJ_ROOT": root}):
            old_cwd = os.getcwd()
            os.chdir(cwd)
            try:
                sl = StepLogger.maybe_create()
            finally:
                os.chdir(old_cwd)
            self.assertIsNotNone(sl)
            self.assertEqual(sl.dir, Path(root) / "user_task" / "steps")
            self.assertTrue(sl.dir.is_dir())
            self.assertEqual(os.listdir(cwd), [])  # nothing leaked into CWD

    def test_relay_traj_dir_still_pins(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ, {"RELAY_TRAJ_DIR": td,
                                          "RELAY_TRAJ_ROOT": "/never/used"}):
            sl = StepLogger.maybe_create()
            self.assertIsNotNone(sl)
            self.assertEqual(sl.dir, Path(td) / "steps")

    def test_step_log_off_returns_none(self):
        with mock.patch.dict(os.environ, {"RELAY_STEP_LOG": "0"}):
            self.assertIsNone(StepLogger.maybe_create())


if __name__ == "__main__":
    unittest.main()
