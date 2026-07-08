"""Device-less tests for the scrcpy streaming capture backend (roadmap P2-S1).

Pins: server/version discovery overrides, the RELAY_CAPTURE_BACKEND opt-in
(default stays exec-out screencap), and the loud permanent fallback when the
stream can't start. The stream itself needs a device — covered by the smoke
run, not unit tests.
"""
from __future__ import annotations

import io
import os
import subprocess
import tempfile
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


if __name__ == "__main__":
    unittest.main()
