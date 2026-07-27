"""Device-less tests for the scrcpy streaming capture backend (roadmap P2-S1)
and the frame-arrival settle detection built on it (P2-S2).

Pins: server/version discovery overrides, the RELAY_CAPTURE_BACKEND opt-in
(default stays exec-out screencap), the loud permanent fallback when the
stream can't start, and AndroidBackend.wait_settled's quiet-window / budget /
dead-stream branches. The stream itself needs a device — covered by the smoke
run, not unit tests.
"""
from __future__ import annotations

import io
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agents.device import android_stream
from agents.device.android import AndroidBackend


class FindServerTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("RELAY_SCRCPY_SERVER", None)
        os.environ.pop("RELAY_SCRCPY_VERSION", None)

    def test_env_path_wins(self) -> None:
        with tempfile.NamedTemporaryFile() as fh:
            os.environ["RELAY_SCRCPY_SERVER"] = fh.name
            self.assertEqual(android_stream.find_server(), Path(fh.name))

    def test_env_path_missing_is_none_not_search(self) -> None:
        # An explicit-but-wrong path must fail loudly (None → warning at the
        # caller), not silently pick up some other install.
        os.environ["RELAY_SCRCPY_SERVER"] = "/nonexistent/scrcpy-server"
        with mock.patch.object(android_stream.shutil, "which", return_value=None):
            self.assertIsNone(android_stream.find_server())

    def test_version_env_override(self) -> None:
        os.environ["RELAY_SCRCPY_VERSION"] = "9.9.9"
        self.assertEqual(android_stream.client_version(), "9.9.9")

    def test_version_parsed_from_cli(self) -> None:
        fake = subprocess.CompletedProcess(
            [], 0, stdout="scrcpy 3.3.4 <https://github.com/Genymobile/scrcpy>\n")
        with mock.patch.object(android_stream.subprocess, "run", return_value=fake):
            self.assertEqual(android_stream.client_version(), "3.3.4")


def _fake_png_run(*args, **kwargs) -> subprocess.CompletedProcess:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="PNG")
    return subprocess.CompletedProcess([], 0, stdout=buf.getvalue(), stderr=b"")


class CaptureBackendSeamTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("RELAY_CAPTURE_BACKEND", None)

    def test_default_never_touches_the_stream(self) -> None:
        backend = AndroidBackend()
        with mock.patch.object(backend, "_run", side_effect=_fake_png_run), \
             mock.patch.object(backend, "_stream_frame") as stream:
            img = backend.screencap()
        self.assertIsNotNone(img)
        stream.assert_not_called()

    def test_scrcpy_start_failure_falls_back_permanently(self) -> None:
        os.environ["RELAY_CAPTURE_BACKEND"] = "scrcpy"
        backend = AndroidBackend()
        with mock.patch(
            "agents.device.android_stream.ScrcpyStream"
        ) as stream_cls, mock.patch.object(backend, "_run", side_effect=_fake_png_run):
            stream_cls.return_value.start.side_effect = RuntimeError("no server")
            img1 = backend.screencap()
            img2 = backend.screencap()
        self.assertIsNotNone(img1)
        self.assertIsNotNone(img2)
        self.assertTrue(backend._capture_stream_failed)
        stream_cls.assert_called_once()  # no restart storm after the first failure

    def test_scrcpy_frames_served_from_stream(self) -> None:
        os.environ["RELAY_CAPTURE_BACKEND"] = "scrcpy"
        backend = AndroidBackend()
        sentinel = object()
        with mock.patch("agents.device.android_stream.ScrcpyStream") as stream_cls:
            stream_cls.return_value.screencap.return_value = sentinel
            stream_cls.return_value.local_port = 12345
            img = backend.screencap()
        self.assertIs(img, sentinel)

    def test_lost_stream_flips_to_execout(self) -> None:
        os.environ["RELAY_CAPTURE_BACKEND"] = "scrcpy"
        backend = AndroidBackend()
        with mock.patch("agents.device.android_stream.ScrcpyStream") as stream_cls, \
             mock.patch.object(backend, "_run", side_effect=_fake_png_run):
            stream_cls.return_value.screencap.return_value = None  # stream died
            stream_cls.return_value.local_port = 12345
            img = backend.screencap()
        self.assertIsNotNone(img)  # exec-out picked it up
        self.assertTrue(backend._capture_stream_failed)
        stream_cls.return_value.close.assert_called_once()


class _FakeStream:
    """Duck-typed ScrcpyStream: wait_settled only touches `alive`,
    `frame_seq` and `wait_for_new_frame(after_seq, timeout)`."""

    def __init__(self, alive: bool = True):
        self.alive = alive
        self.frame_seq = 0
        self.timeouts: list[float] = []
        self.on_wait = None  # callable(stream), runs inside wait_for_new_frame

    def wait_for_new_frame(self, after_seq: int, timeout: float) -> int:
        self.timeouts.append(timeout)
        if self.on_wait is not None:
            self.on_wait(self)
        return self.frame_seq


class WaitSettledTests(unittest.TestCase):
    """P2-S2 settle detection. Contract (CLAUDE.md / DeviceBackend.wait_settled):
    True = the settle was handled here; False = no signal, the CALLER pays its
    fixed sleep — including when the stream is dead."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"RELAY_SETTLE_DETECT": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend = AndroidBackend()

    def _attach(self, stream: _FakeStream) -> _FakeStream:
        self.backend._capture_stream = stream
        return stream

    def test_no_stream_returns_false(self):
        self.assertFalse(self.backend.wait_settled(0.5))

    def test_dead_stream_at_entry_returns_false(self):
        s = self._attach(_FakeStream(alive=False))
        self.assertFalse(self.backend.wait_settled(0.5))
        self.assertEqual(s.timeouts, [])  # never even waited

    def test_detect_disabled_returns_false(self):
        self._attach(_FakeStream())
        with mock.patch.dict(os.environ, {"RELAY_SETTLE_DETECT": "0"}):
            self.assertFalse(self.backend.wait_settled(0.5))

    def test_quiet_window_without_frames_settles(self):
        s = self._attach(_FakeStream())  # no frame arrives during the wait
        self.assertTrue(self.backend.wait_settled(5.0, quiet=0.2))
        self.assertEqual(len(s.timeouts), 1)
        self.assertAlmostEqual(s.timeouts[0], 0.2, places=2)  # full quiet window

    def test_budget_exhaustion_settles_with_trimmed_waits(self):
        # Frames keep arriving (continuous animation): spend the whole budget,
        # then True — identical worst case to the fixed sleep it replaces.
        s = self._attach(_FakeStream())

        def frames_keep_coming(st: _FakeStream) -> None:
            time.sleep(0.005)
            st.frame_seq += 1

        s.on_wait = frames_keep_coming
        self.assertTrue(self.backend.wait_settled(0.05, quiet=0.2))
        self.assertGreaterEqual(len(s.timeouts), 1)
        # every wait is trimmed to min(quiet, remaining budget)
        self.assertTrue(all(t <= 0.05 + 1e-6 for t in s.timeouts))

    def test_stream_dying_mid_wait_returns_false(self):
        # The decode thread flips _alive and notifies: wait_for_new_frame
        # wakes immediately with an UNCHANGED seq. That must report no-signal
        # (False → caller falls back to its fixed sleep), not "settled".
        s = self._attach(_FakeStream())
        s.on_wait = lambda st: setattr(st, "alive", False)
        self.assertFalse(self.backend.wait_settled(5.0, quiet=0.2))
        self.assertEqual(len(s.timeouts), 1)

    def test_death_beats_a_simultaneous_new_frame(self):
        # Even when a last frame lands in the same wakeup, a dead stream is
        # no-signal: liveness is checked before the seq comparison.
        s = self._attach(_FakeStream())

        def die_with_frame(st: _FakeStream) -> None:
            st.frame_seq += 1
            st.alive = False

        s.on_wait = die_with_frame
        self.assertFalse(self.backend.wait_settled(5.0, quiet=0.2))


if __name__ == "__main__":
    unittest.main()
