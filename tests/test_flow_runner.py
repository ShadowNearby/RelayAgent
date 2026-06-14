"""Unit tests for FlowRunner trajectory helpers."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from agents.flow.flow_runner import FlowRunner, _harvest_mw_traj
from agents.flow.leg_judge import final_frames


class FoldFlowLlmCallsTests(unittest.TestCase):
    def test_fold_appends_to_existing_traj(self) -> None:
        leg_dir = Path(self._tmp) / "leg"
        leg_dir.mkdir()
        (leg_dir / "traj.json").write_text(
            json.dumps({"0": {"traj": []}}), encoding="utf-8"
        )
        runner = FlowRunner.__new__(FlowRunner)
        runner._llm = MagicMock()
        runner._llm.calls = [
            {"purpose": "leg_judge", "model": "qwen"},
            {"purpose": "bind_extract", "model": "qwen"},
        ]
        runner._fold_flow_llm_calls(leg_dir, start_idx=1)
        data = json.loads((leg_dir / "traj.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["flow_llm_calls"]), 1)
        self.assertEqual(data["flow_llm_calls"][0]["purpose"], "bind_extract")
        self.assertIn("0", data)

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)


class HarvestMwTrajTests(unittest.TestCase):
    def test_harvests_last_answer_action(self) -> None:
        leg_dir = Path(self._tmp) / "mw_leg"
        traj = {
            "0": {
                "traj": [
                    {"action": {"action_type": "click", "goal_status": "in_progress"}},
                    {"action": {"action_type": "answer", "text": "  done  "}},
                ]
            }
        }
        mw_traj = leg_dir / "user_task" / "traj.json"
        mw_traj.parent.mkdir(parents=True)
        mw_traj.write_text(json.dumps(traj), encoding="utf-8")
        reply, terminal, status = _harvest_mw_traj(leg_dir)
        self.assertEqual(reply, "done")
        self.assertEqual(terminal, "answer")
        self.assertIsNone(status)

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)


class FinalFramesTests(unittest.TestCase):
    def test_prefers_relay_steps_dir(self) -> None:
        leg_dir = Path(self._tmp) / "relay_leg"
        steps = leg_dir / "steps"
        steps.mkdir(parents=True)
        (steps / "step_1.png").write_bytes(b"png")
        (steps / "step_2.png").write_bytes(b"png")
        frames = final_frames(leg_dir, n=2)
        self.assertEqual([p.name for p in frames], ["step_1.png", "step_2.png"])

    def test_falls_back_to_mobileworld_screenshots(self) -> None:
        leg_dir = Path(self._tmp) / "mw_leg"
        shots = leg_dir / "user_task" / "screenshots"
        shots.mkdir(parents=True)
        (shots / "task-0-1.png").write_bytes(b"png")
        (shots / "task-0-3.png").write_bytes(b"png")
        frames = final_frames(leg_dir, n=2)
        self.assertEqual([p.name for p in frames], ["task-0-1.png", "task-0-3.png"])

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)


class NativeRunnerTrajDirTests(unittest.TestCase):
    def test_rotate_skips_backup_when_relay_traj_dir_set(self) -> None:
        leg_dir = Path(self._tmp) / "pinned_leg"
        leg_dir.mkdir()
        marker = leg_dir / "keep_me.txt"
        marker.write_text("stay", encoding="utf-8")
        os.environ["RELAY_TRAJ_DIR"] = str(leg_dir)
        try:
            import agents.runtime.native_runner as nr

            rotated = nr._rotate_traj_dir()
            self.assertEqual(rotated, leg_dir)
            self.assertTrue(marker.exists())
            self.assertTrue((leg_dir / "traj.json").exists())
        finally:
            os.environ.pop("RELAY_TRAJ_DIR", None)

    def test_traj_dir_resolved_per_call_not_at_import(self) -> None:
        # In-process legs change RELAY_TRAJ_DIR between calls in ONE process;
        # the resolution must follow the env, not a value frozen at import.
        import agents.runtime.native_runner as nr

        leg_a = Path(self._tmp) / "leg_a"
        leg_b = Path(self._tmp) / "leg_b"
        try:
            os.environ["RELAY_TRAJ_DIR"] = str(leg_a)
            self.assertEqual(nr._rotate_traj_dir(), leg_a)
            os.environ["RELAY_TRAJ_DIR"] = str(leg_b)
            self.assertEqual(nr._rotate_traj_dir(), leg_b)
            self.assertTrue((leg_b / "traj.json").exists())
        finally:
            os.environ.pop("RELAY_TRAJ_DIR", None)

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        os.environ.pop("RELAY_TRAJ_DIR", None)
        shutil.rmtree(self._tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
