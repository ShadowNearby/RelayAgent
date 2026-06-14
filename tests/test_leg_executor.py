"""Tests for flow_runner's leg executors (subprocess seam + in-process mode).

The in-process executor must give each leg exactly the env the flow built
for it (the RELAY_* env-var contract is the leg API) and restore the
caller's env afterwards — including on crash — since one process runs many
legs sequentially on Android.
"""
from __future__ import annotations

import os
import unittest

import agents.runtime.native_runner as native_runner
from agents.flow.flow_runner import (
    InProcessLegExecutor,
    SubprocessLegExecutor,
    _default_leg_executor,
)


class InProcessLegExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_main = native_runner.main
        self.seen_envs: list[dict] = []

    def tearDown(self) -> None:
        native_runner.main = self._orig_main
        os.environ.pop("RELAY_LEG_EXECUTOR", None)

    def _fake_main(self, rc=0, exc=None):
        def fake(argv):
            self.seen_envs.append(dict(os.environ))
            if exc is not None:
                raise exc
            return rc

        native_runner.main = fake

    def test_leg_sees_exactly_child_env_and_caller_env_restored(self) -> None:
        self._fake_main()
        os.environ["RELAY_TEST_SENTINEL"] = "host"
        try:
            child_env = {"PATH": os.environ.get("PATH", ""),
                         "RELAY_TARGET_APP": "com.example.a"}
            rc = InProcessLegExecutor().run("com.example.a", "goal", child_env, [])
            self.assertEqual(rc, 0)
            # The leg saw the flow-built env verbatim — no host leakage.
            self.assertEqual(self.seen_envs[0], child_env)
            # And the caller's env came back, leg keys gone.
            self.assertEqual(os.environ.get("RELAY_TEST_SENTINEL"), "host")
            self.assertNotIn("RELAY_TARGET_APP", os.environ)
        finally:
            os.environ.pop("RELAY_TEST_SENTINEL", None)

    def test_sequential_legs_are_isolated(self) -> None:
        self._fake_main()
        ex = InProcessLegExecutor()
        ex.run("a", "g", {"RELAY_TARGET_APP": "com.example.a"}, [])
        ex.run("b", "g", {"RELAY_TARGET_APP": "com.example.b"}, [])
        self.assertEqual(self.seen_envs[0]["RELAY_TARGET_APP"], "com.example.a")
        self.assertEqual(self.seen_envs[1]["RELAY_TARGET_APP"], "com.example.b")
        self.assertNotIn("RELAY_TARGET_APP", os.environ)

    def test_system_exit_message_maps_to_rc1(self) -> None:
        # native_runner.main maps config errors to sys.exit(str(...)).
        self._fake_main(exc=SystemExit("LLM_BASE_URL missing"))
        rc = InProcessLegExecutor().run("a", "g", {}, [])
        self.assertEqual(rc, 1)

    def test_system_exit_code_passes_through(self) -> None:
        self._fake_main(exc=SystemExit(3))
        rc = InProcessLegExecutor().run("a", "g", {}, [])
        self.assertEqual(rc, 3)

    def test_crash_returns_rc1_and_restores_env(self) -> None:
        self._fake_main(exc=ValueError("boom"))
        os.environ["RELAY_TEST_SENTINEL"] = "host"
        try:
            rc = InProcessLegExecutor().run("a", "g", {"X": "1"}, [])
            self.assertEqual(rc, 1)
            self.assertEqual(os.environ.get("RELAY_TEST_SENTINEL"), "host")
            self.assertNotIn("X", os.environ)
        finally:
            os.environ.pop("RELAY_TEST_SENTINEL", None)

    def test_extra_args_forwarded_to_argv(self) -> None:
        captured: list[list[str]] = []

        def fake(argv):
            captured.append(list(argv))
            return 0

        native_runner.main = fake
        InProcessLegExecutor().run("app", "goal", {}, ["--max-step", "5"])
        self.assertEqual(captured[0], ["app", "goal", "--max-step", "5"])


class DefaultExecutorSelectionTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("RELAY_LEG_EXECUTOR", None)

    def test_default_is_subprocess(self) -> None:
        os.environ.pop("RELAY_LEG_EXECUTOR", None)
        self.assertIsInstance(_default_leg_executor(), SubprocessLegExecutor)

    def test_inprocess_opt_in(self) -> None:
        os.environ["RELAY_LEG_EXECUTOR"] = "inprocess"
        self.assertIsInstance(_default_leg_executor(), InProcessLegExecutor)


if __name__ == "__main__":
    unittest.main()
