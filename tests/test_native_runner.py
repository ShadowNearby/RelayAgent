"""Device-less tests for run_leg's IME lifecycle protection.

Pins two failure-path contracts:
- a teardown_input_channel failure in the finally (adb half-dead → `ime
  reset` TimeoutExpired) never masks the run's real outcome and never skips
  the RELAY_SUMMARY_OUT write;
- the non-ASCII fast-fail (env_fail) raises BEFORE the try/finally, so it
  must reset the IME itself (respecting --keep-ime) instead of leaving
  AdbKeyboard enabled on the device.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents.runtime import native_runner


class _FakeBackend:
    def __init__(self, *, setup_ok: bool = True, teardown_exc: Exception | None = None):
        self._setup_ok = setup_ok
        self._teardown_exc = teardown_exc
        self.teardown_calls = 0

    def setup_input_channel(self) -> bool:
        return self._setup_ok

    def teardown_input_channel(self) -> None:
        self.teardown_calls += 1
        if self._teardown_exc is not None:
            raise self._teardown_exc


class _FakeAgent:
    def __init__(self, **kwargs):
        pass

    def get_total_token_usage(self):
        return {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


class RunLegTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.summary_out = Path(self.tmp.name) / "summary.json"
        # Pin the traj dir into the tempdir so _rotate_traj_dir never touches
        # the repo's live traj_logs/; patch.dict also restores every env key
        # run_leg mutates (RELAY_TARGET_APP, RELAY_SKIP_OPEN_APP, ...).
        env_patch = mock.patch.dict(os.environ, {
            "RELAY_TRAJ_DIR": str(Path(self.tmp.name) / "leg"),
            "RELAY_SUMMARY_OUT": str(self.summary_out),
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)

        cfg = mock.patch.object(
            native_runner, "resolve_llm_config",
            return_value=({}, "http://llm", "key", "model"),
        )
        cfg.start()
        self.addCleanup(cfg.stop)

        loader = mock.patch.object(
            native_runner, "_load_agent_class", return_value=_FakeAgent
        )
        loader.start()
        self.addCleanup(loader.stop)

    def _run(self, backend, goal="ascii goal", *, keep_ime=False,
             run_task_exc=None):
        with mock.patch("agents.device.get_backend", return_value=backend), \
             mock.patch("agents.runtime.native_runtime.run_task") as run_task:
            if run_task_exc is not None:
                run_task.side_effect = run_task_exc
            else:
                run_task.return_value = {"steps": 1}
            return native_runner.run_leg("com.example.app", goal, keep_ime=keep_ime)

    def test_teardown_failure_does_not_mask_success(self):
        backend = _FakeBackend(
            teardown_exc=subprocess.TimeoutExpired(cmd="adb", timeout=30)
        )
        summary = self._run(backend)  # must not raise
        self.assertEqual(summary["steps"], 1)
        self.assertEqual(backend.teardown_calls, 1)
        # The summary write after the teardown still ran, with token backfill.
        data = json.loads(self.summary_out.read_text(encoding="utf-8"))
        self.assertEqual(data["token_usage"]["total_tokens"], 3)

    def test_teardown_failure_does_not_mask_original_error(self):
        backend = _FakeBackend(
            teardown_exc=subprocess.TimeoutExpired(cmd="adb", timeout=30)
        )
        with self.assertRaises(RuntimeError) as ctx:
            self._run(backend, run_task_exc=RuntimeError("device exploded"))
        self.assertIn("device exploded", str(ctx.exception))
        self.assertEqual(backend.teardown_calls, 1)
        self.assertTrue(self.summary_out.exists())  # summary not dropped

    def test_keep_ime_skips_teardown(self):
        backend = _FakeBackend()
        self._run(backend, keep_ime=True)
        self.assertEqual(backend.teardown_calls, 0)

    def test_non_ascii_fast_fail_resets_ime(self):
        backend = _FakeBackend(setup_ok=False)
        with mock.patch("agents.device.get_backend", return_value=backend), \
             mock.patch("agents.runtime.native_runtime.run_task") as run_task:
            with self.assertRaises(RuntimeError) as ctx:
                native_runner.run_leg("com.example.app", "帮我点一杯咖啡")
        self.assertIn("env_fail", str(ctx.exception))
        self.assertEqual(backend.teardown_calls, 1)  # IME restored before raise
        run_task.assert_not_called()

    def test_non_ascii_fast_fail_respects_keep_ime(self):
        backend = _FakeBackend(setup_ok=False)
        with mock.patch("agents.device.get_backend", return_value=backend), \
             mock.patch("agents.runtime.native_runtime.run_task"):
            with self.assertRaises(RuntimeError):
                native_runner.run_leg(
                    "com.example.app", "帮我点一杯咖啡", keep_ime=True
                )
        self.assertEqual(backend.teardown_calls, 0)

    def test_fast_fail_teardown_error_does_not_mask_env_fail(self):
        backend = _FakeBackend(setup_ok=False, teardown_exc=OSError("adb gone"))
        with mock.patch("agents.device.get_backend", return_value=backend), \
             mock.patch("agents.runtime.native_runtime.run_task"):
            with self.assertRaises(RuntimeError) as ctx:
                native_runner.run_leg("com.example.app", "帮我点一杯咖啡")
        self.assertIn("env_fail", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
