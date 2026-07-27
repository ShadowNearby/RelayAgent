"""Device-less tests for the screenrecord chunk recorder.

Pins: (1) consecutive instant screenrecord failures make the capture loop
give up after _MAX_FAST_FAILS (one warning) instead of a ~1.5s Popen+pull
restart storm for the rest of the run; (2) an explicit `adb_base` argv
prefix (AndroidBackend passes its instance's, carrying the per-instance
serial) reaches every adb invocation the loop makes.
"""
from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agents.runtime import _recorder


def _cp(argv=(), returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(list(argv), returncode, stdout="", stderr=stderr)


class FastFailGiveUpTests(unittest.TestCase):
    def test_instant_failures_give_up_after_max(self):
        proc = mock.Mock()
        proc.wait.return_value = 1  # screenrecord dies immediately
        proc.poll.return_value = 1
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(_recorder, "_default_adb_base", return_value=["adb"]), \
             mock.patch.object(_recorder.subprocess, "Popen", return_value=proc) as popen, \
             mock.patch.object(_recorder.subprocess, "run",
                               return_value=_cp(returncode=1, stderr="pull failed")), \
             mock.patch.object(_recorder.time, "sleep"):
            rec = _recorder.start(Path(td))
            rec._thread.join(timeout=10)
            self.assertFalse(rec._thread.is_alive(),
                             "loop must give up, not spin until stop()")
            self.assertEqual(popen.call_count, _recorder._MAX_FAST_FAILS)
            self.assertEqual(rec._chunks, [])
            self.assertIsNone(rec.stop())  # no chunks → clean None

    def test_a_successful_chunk_resets_the_fail_counter(self):
        proc = mock.Mock()
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        pulls = {"n": 0}

        def fake_run(argv, **kwargs):
            if "pull" in argv:
                pulls["n"] += 1
                if pulls["n"] == 3:  # third chunk records fine
                    Path(argv[-1]).write_bytes(b"x" * 32)
                    return _cp(argv)
                return _cp(argv, returncode=1, stderr="nope")
            return _cp(argv)

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(_recorder, "_default_adb_base", return_value=["adb"]), \
             mock.patch.object(_recorder.subprocess, "Popen", return_value=proc) as popen, \
             mock.patch.object(_recorder.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(_recorder.time, "sleep"):
            rec = _recorder.start(Path(td))
            rec._thread.join(timeout=10)
            self.assertFalse(rec._thread.is_alive())
            # 2 fast fails + 1 success (counter reset) + 3 fast fails → 6 chunks
            self.assertEqual(popen.call_count, 6)
            self.assertEqual(len(rec._chunks), 1)


class AdbBasePassThroughTests(unittest.TestCase):
    def test_explicit_adb_base_reaches_all_adb_calls(self):
        base = ["adb", "-s", "SERIAL-B"]
        holder: dict = {}
        gate = threading.Event()
        proc = mock.Mock()
        proc.poll.return_value = 0

        def wait_then_stop(*args, **kwargs):
            gate.wait(timeout=10)
            holder["rec"]._stop_evt.set()  # stop after the first chunk
            return 0

        proc.wait.side_effect = wait_then_stop
        run_argvs: list[list[str]] = []

        def fake_run(argv, **kwargs):
            run_argvs.append(list(argv))
            if "pull" in argv:
                Path(argv[-1]).write_bytes(b"x" * 32)
            return _cp(argv)

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(_recorder.subprocess, "Popen", return_value=proc) as popen, \
             mock.patch.object(_recorder.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(_recorder.time, "sleep"):
            rec = _recorder.start(Path(td), adb_base=base)
            holder["rec"] = rec
            gate.set()
            rec._thread.join(timeout=10)
            self.assertFalse(rec._thread.is_alive())
            # screenrecord Popen and pull/rm runs all carry the caller's prefix
            self.assertEqual(popen.call_args.args[0][:3], base)
            self.assertTrue(run_argvs)
            for argv in run_argvs:
                self.assertEqual(argv[:3], base)
            self.assertEqual(len(rec._chunks), 1)


if __name__ == "__main__":
    unittest.main()
