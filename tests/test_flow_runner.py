"""Unit tests for FlowRunner trajectory helpers."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
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


class ExtractTests(unittest.TestCase):
    """Pins the bind/extract data path: _extract must RETURN the parsed value
    (a refactor once dropped the trailing `return data`, silently binding None
    into the blackboard for every extract step)."""

    def _runner(self, content: str) -> FlowRunner:
        runner = FlowRunner.__new__(FlowRunner)
        runner.bb = {}
        runner.env = {"LLM_MODEL": "qwen"}
        resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )
        runner._llm = MagicMock()
        runner._llm.chat.completions.create.return_value = resp
        return runner

    def test_extract_returns_parsed_json(self) -> None:
        runner = self._runner('```json\n{"items": [{"name": "A"}, {"name": "B"}]}\n```')
        out = runner._extract("raw reply", {"prompt": "parse", "bind_to_array_key": "items"})
        self.assertEqual(out, [{"name": "A"}, {"name": "B"}])

    def test_extract_without_array_key_returns_object(self) -> None:
        runner = self._runner('```json\n{"city": "上海"}\n```')
        out = runner._extract("raw reply", {"prompt": "parse"})
        self.assertEqual(out, {"city": "上海"})

    def test_extract_parse_failure_degrades_to_raw_text(self) -> None:
        # The leg already succeeded; a prose/truncated extractor reply at
        # commit time must degrade to binding the raw reply, not raise.
        runner = self._runner("抱歉，这不是 JSON。")
        out = runner._extract("原始回复文本", {"prompt": "parse"})
        self.assertEqual(out, "原始回复文本")

    def test_extract_truncated_fence_degrades_to_raw_text(self) -> None:
        runner = self._runner('```json\n{"items": [{"name": "A"}, {"na')
        out = runner._extract("原始回复文本", {"prompt": "parse", "bind_to_array_key": "items"})
        self.assertEqual(out, "原始回复文本")

    def test_extract_missing_prompt_does_not_keyerror(self) -> None:
        runner = self._runner('```json\n{"a": 1}\n```')
        out = runner._extract("raw reply", {})
        self.assertEqual(out, {"a": 1})


class MobileworldStepTests(unittest.TestCase):
    """Runs one MW fallback leg end-to-end with the driver subprocess mocked.
    Pins the command construction (a refactor once dropped `import sys`,
    NameError-ing every MW leg), the harvest→bind path, and the output-free
    terminal check (a timed-out/crashed MW run must fail the leg, not slide
    through as success)."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.mkdtemp()
        os.environ["RELAY_LEG_JUDGE"] = "0"  # judging needs a device + LLM

    def tearDown(self) -> None:
        import shutil

        os.environ.pop("RELAY_LEG_JUDGE", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _mw_runner(self) -> FlowRunner:
        runner = FlowRunner.__new__(FlowRunner)
        runner.bb = {}
        runner.env = {}
        runner._llm = MagicMock(calls=[])
        runner.flow_traj_root = Path(self._tmp)
        runner._step_idx = 0
        runner._mw_server_url = "http://127.0.0.1:1"
        runner._mw_server_proc = object()  # pretend the flow already started one
        return runner

    def _write_mw_traj(self, leg_dirname: str, actions: list[dict]) -> None:
        traj = Path(self._tmp) / leg_dirname / "user_task" / "traj.json"
        traj.parent.mkdir(parents=True, exist_ok=True)
        traj.write_text(
            json.dumps({"0": {"traj": [{"action": a} for a in actions]}}),
            encoding="utf-8",
        )

    def test_mw_step_builds_command_and_binds_answer(self) -> None:
        runner = self._mw_runner()

        def fake_call(cmd, **kwargs):
            # The driver would write MobileWorld's traj; emulate its answer.
            self._write_mw_traj(
                "01_mw1", [{"action_type": "answer", "text": "the answer"}]
            )
            return 0

        with mock.patch(
            "agents.flow.flow_runner.subprocess.call", side_effect=fake_call
        ) as call:
            runner._run_mobileworld_step({"id": "mw1", "prompt": "do it", "bind": "out"})

        self.assertEqual(runner.bb["out"], "the answer")
        argv = call.call_args[0][0]
        self.assertEqual(argv[0], sys.executable)
        self.assertIn("--no-start-server", argv)
        self.assertIn("--no-prelaunch", argv)

    def test_mw_output_free_step_crash_raises(self) -> None:
        # Whole-request fallback shape (_mw_whole_request_plan): no bind. A
        # driver that dies without writing a traj must fail the leg.
        runner = self._mw_runner()
        with mock.patch(
            "agents.flow.flow_runner.subprocess.call", return_value=124
        ), self.assertRaisesRegex(RuntimeError, "terminal state"):
            runner._run_mobileworld_step({"id": "mw_fallback", "prompt": "do it"})

    def test_mw_output_free_step_non_terminal_traj_raises(self) -> None:
        # rc=0 but the traj never reached a terminal action (e.g. max-round
        # exhausted mid-task): the output-free leg must not read as success.
        runner = self._mw_runner()

        def fake_call(cmd, **kwargs):
            self._write_mw_traj(
                "01_mw_fallback",
                [{"action_type": "click", "goal_status": "in_progress"}],
            )
            return 0

        with mock.patch(
            "agents.flow.flow_runner.subprocess.call", side_effect=fake_call
        ), self.assertRaisesRegex(RuntimeError, "terminal state"):
            runner._run_mobileworld_step({"id": "mw_fallback", "prompt": "do it"})

    def test_mw_output_free_step_terminal_state_passes(self) -> None:
        runner = self._mw_runner()

        def fake_call(cmd, **kwargs):
            self._write_mw_traj(
                "01_mw_fallback",
                [{"action_type": "finished", "goal_status": "complete"}],
            )
            return 0

        with mock.patch(
            "agents.flow.flow_runner.subprocess.call", side_effect=fake_call
        ):
            runner._run_mobileworld_step({"id": "mw_fallback", "prompt": "do it"})
        self.assertEqual(runner.bb, {})  # output-free: nothing bound


class GeneralStepTests(unittest.TestCase):
    """Runs one general fallback leg with the leg executor mocked. Pins the
    reply→bind path and the output-free terminal check (mirrors the MW leg)."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.mkdtemp()
        os.environ["RELAY_LEG_JUDGE"] = "0"  # judging needs a device + LLM

    def tearDown(self) -> None:
        import shutil

        os.environ.pop("RELAY_LEG_JUDGE", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _general_runner(self) -> FlowRunner:
        runner = FlowRunner.__new__(FlowRunner)
        runner.bb = {}
        runner.env = {}
        runner._llm = MagicMock(calls=[])
        runner.flow_traj_root = Path(self._tmp)
        runner._step_idx = 0
        runner.extra_args = []
        runner._leg_executor = MagicMock()
        return runner

    @staticmethod
    def _fake_run(summary: dict | None, reply: str = "", rc: int = 0):
        def run(target, prompt, child_env, extra_args):
            if summary is not None:
                Path(child_env["RELAY_SUMMARY_OUT"]).write_text(
                    json.dumps(summary), encoding="utf-8"
                )
            if reply:
                Path(child_env["RELAY_REPLY_OUT"]).write_text(
                    json.dumps({"reply": reply}), encoding="utf-8"
                )
            return rc

        return run

    def test_general_step_binds_answer(self) -> None:
        runner = self._general_runner()
        runner._leg_executor.run.side_effect = self._fake_run(
            {"last_action_type": "answer"}, reply="the answer"
        )
        runner._run_general_step({"id": "g1", "prompt": "do it", "bind": "out"})
        self.assertEqual(runner.bb["out"], "the answer")

    def test_general_output_free_step_crash_raises(self) -> None:
        # Whole-request fallback shape (_general_whole_request_plan): no bind.
        # A run that dies before writing a summary must fail the leg.
        runner = self._general_runner()
        runner._leg_executor.run.side_effect = self._fake_run(None, rc=1)
        with self.assertRaisesRegex(RuntimeError, "terminal state"):
            runner._run_general_step({"id": "g1", "prompt": "do it"})

    def test_general_output_free_step_non_terminal_summary_raises(self) -> None:
        runner = self._general_runner()
        runner._leg_executor.run.side_effect = self._fake_run(
            {"last_action_type": "click", "last_goal_status": None}
        )
        with self.assertRaisesRegex(RuntimeError, "terminal state"):
            runner._run_general_step({"id": "g1", "prompt": "do it"})

    def test_general_output_free_step_terminal_state_passes(self) -> None:
        runner = self._general_runner()
        runner._leg_executor.run.side_effect = self._fake_run(
            {"last_action_type": "finished", "last_goal_status": "complete"}
        )
        runner._run_general_step({"id": "g1", "prompt": "do it"})
        self.assertEqual(runner.bb, {})  # output-free: nothing bound


class FlowReportStepCoverageTests(unittest.TestCase):
    """flow_report.json `steps` must cover EVERY plan step — plan-time MW /
    general fallback legs and ask_user steps included, success and failure —
    not just app steps (the benchmark harvest reads these rows; rows are only
    added, never changed, so existing consumers keep working)."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.mkdtemp()
        os.environ["RELAY_PROFILE_ROOT"] = self._tmp  # hermetic profile store

    def tearDown(self) -> None:
        import shutil

        os.environ.pop("RELAY_PROFILE_ROOT", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _runner(self, steps: list[dict]) -> FlowRunner:
        from agents.flow.leg_recovery import RecoveryController

        runner = FlowRunner.__new__(FlowRunner)
        runner.flow = {"steps": steps}
        runner.flow_path = Path(self._tmp) / "flow.yaml"
        runner.bb = {}
        runner.env = {"LLM_MODEL": "qwen"}
        runner._llm = MagicMock(calls=[])
        runner.flow_traj_root = Path(self._tmp)
        runner._step_idx = 0
        runner._step_outcomes = []
        runner._recovery = RecoveryController(runner._llm, "qwen")
        runner._mw_server_proc = None
        runner._mw_server_log = None
        return runner

    def test_mw_general_and_ask_user_steps_are_reported(self) -> None:
        runner = self._runner([
            {"id": "a", "type": "ask_user", "bind": "x"},
            {"id": "m", "type": "mobileworld", "prompt": "p"},
            {"id": "g", "type": "general", "prompt": "p"},
        ])
        runner._run_ask_user = MagicMock()
        runner._run_mobileworld_step = MagicMock()
        runner._run_general_step = MagicMock()
        with mock.patch("agents.flow.flow_runner.get_interaction") as gi:
            gi.return_value.should_stop.return_value = False
            runner.run()
        self.assertEqual(
            [(o["step"], o["status"]) for o in runner._step_outcomes],
            [("a", "ok"), ("m", "ok"), ("g", "ok")],
        )
        report = json.loads(
            (Path(self._tmp) / "flow_report.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(report["steps"]), 3)

    def test_failed_mw_step_is_reported_with_reason(self) -> None:
        runner = self._runner([{"id": "m", "type": "mobileworld", "prompt": "p"}])
        runner._run_mobileworld_step = MagicMock(side_effect=RuntimeError("boom"))
        with mock.patch("agents.flow.flow_runner.get_interaction") as gi:
            gi.return_value.should_stop.return_value = False
            with self.assertRaisesRegex(RuntimeError, "boom"):
                runner.run()
        outcome = runner._step_outcomes[-1]
        self.assertEqual(outcome["status"], "failed")
        self.assertIn("boom", outcome["failure_reason"])
        # The report is still written on a mid-flow abort.
        report = json.loads(
            (Path(self._tmp) / "flow_report.json").read_text(encoding="utf-8")
        )
        self.assertEqual(report["steps"][0]["status"], "failed")


class AskUserSelectFromTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        # _run_ask_user loads AND writes the user profile (M2③ choice memory):
        # point the store at a throwaway dir so tests never touch the
        # developer's real ~/.relayagent/profile.yaml.
        self._tmp = tempfile.mkdtemp()
        os.environ["RELAY_PROFILE_ROOT"] = self._tmp

    def tearDown(self) -> None:
        import shutil

        os.environ.pop("RELAY_PROFILE_ROOT", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    @staticmethod
    def _bare_runner(bb: dict) -> FlowRunner:
        runner = FlowRunner.__new__(FlowRunner)
        runner.bb = bb
        return runner

    def test_select_from_path_normalization(self) -> None:
        from agents.flow.flow_runner_util import _select_from_path

        self.assertEqual(_select_from_path("pois"), ["pois"])
        self.assertEqual(_select_from_path("{pois}"), ["pois"])
        self.assertEqual(_select_from_path("{pois}.field"), ["pois", "field"])
        self.assertEqual(_select_from_path(" { pois } . field "), ["pois", "field"])

    def test_select_from_tolerates_braced_var(self) -> None:
        # The validator accepts `{var}` spellings; the runner must resolve the
        # same way instead of using the braced string as a blackboard key.
        runner = self._bare_runner({"pois": [{"name": "A"}, {"name": "B"}]})
        with mock.patch("agents.flow.flow_runner.get_interaction") as gi:
            gi.return_value.ask_user.return_value = "2"
            runner._run_ask_user({
                "id": "pick", "type": "ask_user", "bind": "choice",
                "select_from": "{pois}", "prompt_header": "pick one",
                "item_label": "{name}",
            })
        self.assertEqual(runner.bb["choice"], {"name": "B"})

    def test_string_items_get_usable_labels_and_text_matching(self) -> None:
        # An extract can bind a plain string list; the default "{name}" label
        # template must fall back to the item text (not render empty lines),
        # and text input must match against that fallback label.
        runner = self._bare_runner({"shops": ["优衣库", "海澜之家"]})
        with mock.patch("agents.flow.flow_runner.get_interaction") as gi:
            gi.return_value.ask_user.return_value = "海澜"
            runner._run_ask_user({
                "id": "pick", "type": "ask_user", "bind": "choice",
                "select_from": "shops", "prompt_header": "选哪家?",
            })
        self.assertEqual(runner.bb["choice"], "海澜之家")
        menu = gi.return_value.ask_user.call_args[0][0]
        self.assertIn("1. 优衣库", menu)
        self.assertIn("2. 海澜之家", menu)

    def test_unresolvable_choice_reasks_then_falls_back_to_default(self) -> None:
        # A typo must not abort the flow: one re-ask, then the default pick.
        runner = self._bare_runner({"pois": [{"name": "A"}, {"name": "B"}]})
        with mock.patch("agents.flow.flow_runner.get_interaction") as gi:
            gi.return_value.ask_user.side_effect = ["zzz", "yyy"]
            runner._run_ask_user({
                "id": "pick", "type": "ask_user", "bind": "choice",
                "select_from": "pois", "prompt_header": "pick one",
                "item_label": "{name}",
            })
        self.assertEqual(runner.bb["choice"], {"name": "A"})
        self.assertEqual(gi.return_value.ask_user.call_count, 2)

    def test_reask_after_unresolvable_choice_can_resolve(self) -> None:
        runner = self._bare_runner({"pois": [{"name": "A"}, {"name": "B"}]})
        with mock.patch("agents.flow.flow_runner.get_interaction") as gi:
            gi.return_value.ask_user.side_effect = ["zzz", "2"]
            runner._run_ask_user({
                "id": "pick", "type": "ask_user", "bind": "choice",
                "select_from": "pois", "prompt_header": "pick one",
                "item_label": "{name}",
            })
        self.assertEqual(runner.bb["choice"], {"name": "B"})

    def _write_profile(self, data: dict) -> None:
        import yaml

        Path(self._tmp, "profile.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
        )

    def test_profile_preselects_remembered_choice(self) -> None:
        # M2③: a remembered pick moves the empty-input default onto itself.
        self._write_profile({"last_choices": {"pick one": "B"}})
        runner = self._bare_runner({"pois": [{"name": "A"}, {"name": "B"}]})
        with mock.patch("agents.flow.flow_runner.get_interaction") as gi:
            gi.return_value.ask_user.return_value = ""  # keep the default
            runner._run_ask_user({
                "id": "pick", "type": "ask_user", "bind": "choice",
                "select_from": "pois", "prompt_header": "pick one",
                "item_label": "{name}",
            })
        self.assertEqual(runner.bb["choice"], {"name": "B"})
        menu = gi.return_value.ask_user.call_args[0][0]
        self.assertIn("empty to pick 2", menu)

    def test_explicit_choice_is_written_back_to_profile(self) -> None:
        # M2③ write-back: the user's own explicit pick (not an inference) is
        # recorded so the next run of the same question defaults to it.
        import yaml

        self._write_profile({})  # store exists — the layer is active
        runner = self._bare_runner({"pois": [{"name": "A"}, {"name": "B"}]})
        with mock.patch("agents.flow.flow_runner.get_interaction") as gi:
            gi.return_value.ask_user.return_value = "2"
            runner._run_ask_user({
                "id": "pick", "type": "ask_user", "bind": "choice",
                "select_from": "pois", "prompt_header": "pick one",
                "item_label": "{name}",
            })
        stored = yaml.safe_load(
            Path(self._tmp, "profile.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(stored["last_choices"]["pick one"], "B")


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
