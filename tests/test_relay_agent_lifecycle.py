"""Pin RelayAgent's logging/lifecycle seams and the agent_base LLM-param
handling:

- RELAY_TRAJ_DIR is resolved PER CALL (not frozen at import): in-process
  multi-leg runs re-point it between legs while agents.agent.relay_agent
  stays cached in sys.modules; _append_llm_call and _maybe_persist_reply
  must follow the env, or leg B's telemetry lands in leg A's dir.
- _append_llm_call writes traj.json via temp file + os.replace (atomic; no
  stray .tmp), and stays a silent no-op on a corrupted traj.json.
- atexit registration of _finalize_task is bounded (once per task) and the
  entry is unregistered on finalize, so long-lived in-process runs don't
  accumulate zombie callbacks pinning finished agents. The agent stays the
  sole writer of wall_clock.json.
- agent_base's claude branch matches case-insensitively and never overrides
  the caller's explicit max_tokens/temperature (grounding passes
  max_tokens=128 / temperature=0.0 deliberately).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agents.agent.agent_base import MCPAgent
from agents.agent.relay_agent import RelayAgent

MOD = "agents.agent.relay_agent"


class _TrajDirBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir_a = Path(self.tmp.name) / "leg_a"
        self.dir_b = Path(self.tmp.name) / "leg_b"
        for d in (self.dir_a, self.dir_b):
            d.mkdir()
            (d / "traj.json").write_text("{}", encoding="utf-8")
        self._saved = os.environ.get("RELAY_TRAJ_DIR")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RELAY_TRAJ_DIR", None)
        else:
            os.environ["RELAY_TRAJ_DIR"] = self._saved

    @staticmethod
    def _calls(d: Path) -> list:
        data = json.loads((d / "traj.json").read_text(encoding="utf-8"))
        return data.get("0", {}).get("llm_calls", [])


class TrajDirPerCallTests(_TrajDirBase):
    def test_append_follows_env_between_calls(self):
        agent = RelayAgent.__new__(RelayAgent)  # _append_llm_call needs no state
        os.environ["RELAY_TRAJ_DIR"] = str(self.dir_a)
        agent._append_llm_call({"purpose": "leg-a"})
        os.environ["RELAY_TRAJ_DIR"] = str(self.dir_b)
        agent._append_llm_call({"purpose": "leg-b"})

        self.assertEqual([c["purpose"] for c in self._calls(self.dir_a)], ["leg-a"])
        self.assertEqual([c["purpose"] for c in self._calls(self.dir_b)], ["leg-b"])

    def test_persist_reply_follows_env(self):
        agent = RelayAgent.__new__(RelayAgent)
        agent._last_agent_reply = "答案"
        agent.target_app = "com.example.app"
        saved_out = os.environ.pop("RELAY_REPLY_OUT", None)
        try:
            os.environ["RELAY_TRAJ_DIR"] = str(self.dir_b)
            agent._maybe_persist_reply()
        finally:
            if saved_out is not None:
                os.environ["RELAY_REPLY_OUT"] = saved_out
        self.assertFalse((self.dir_a / "agent_reply.json").exists())
        doc = json.loads((self.dir_b / "agent_reply.json").read_text(encoding="utf-8"))
        self.assertEqual(doc, {"reply": "答案", "target_app": "com.example.app"})


class TrajDirDefaultTests(_TrajDirBase):
    def test_default_matches_runtime_helper(self):
        # traj.json / agent_reply.json must resolve to the SAME shared default
        # as the runner and StepLogger (RELAY_TRAJ_ROOT-aware, repo-anchored),
        # or a run's logs split across two directories.
        from agents.agent.relay_agent import _traj_dir
        from agents.runtime.native_runtime import default_traj_dir

        os.environ.pop("RELAY_TRAJ_DIR", None)
        self.assertEqual(_traj_dir(), default_traj_dir())
        with mock.patch.dict(os.environ, {"RELAY_TRAJ_ROOT": self.tmp.name}):
            self.assertEqual(_traj_dir(), Path(self.tmp.name) / "user_task")

    def test_pinned_env_wins(self):
        from agents.agent.relay_agent import _traj_dir

        os.environ["RELAY_TRAJ_DIR"] = str(self.dir_b)
        self.assertEqual(_traj_dir(), self.dir_b)


class AtomicAppendTests(_TrajDirBase):
    def test_no_tmp_leftover_and_structure_preserved(self):
        agent = RelayAgent.__new__(RelayAgent)
        os.environ["RELAY_TRAJ_DIR"] = str(self.dir_a)
        agent._append_llm_call({"purpose": "p1"})
        agent._append_llm_call({"purpose": "p2"})
        self.assertEqual([p.name for p in self.dir_a.iterdir()], ["traj.json"])
        data = json.loads((self.dir_a / "traj.json").read_text(encoding="utf-8"))
        bucket = data["0"]
        self.assertIsNone(bucket["tools"])
        self.assertEqual(bucket["traj"], [])
        self.assertEqual(len(bucket["llm_calls"]), 2)

    def test_corrupted_traj_is_silent_noop(self):
        agent = RelayAgent.__new__(RelayAgent)
        os.environ["RELAY_TRAJ_DIR"] = str(self.dir_a)
        (self.dir_a / "traj.json").write_text('{"0": {"llm_c', encoding="utf-8")
        agent._append_llm_call({"purpose": "p"})  # must not raise
        self.assertEqual(
            (self.dir_a / "traj.json").read_text(encoding="utf-8"), '{"0": {"llm_c'
        )

    def test_missing_traj_is_noop(self):
        agent = RelayAgent.__new__(RelayAgent)
        os.environ["RELAY_TRAJ_DIR"] = str(self.dir_a)
        (self.dir_a / "traj.json").unlink()
        agent._append_llm_call({"purpose": "p"})
        self.assertFalse((self.dir_a / "traj.json").exists())


def _bare_lifecycle_agent() -> RelayAgent:
    agent = RelayAgent.__new__(RelayAgent)
    agent.record_dir = None
    agent.agent_launch = False
    agent.target_app = None
    agent._task_started = False
    agent._task_t0 = None
    agent._recorder = None
    return agent


class AtexitLifecycleTests(unittest.TestCase):
    def test_register_once_per_task(self):
        agent = _bare_lifecycle_agent()
        with mock.patch(f"{MOD}.atexit") as ax:
            agent._begin_task_once()
            agent._begin_task_once()  # idempotent — body runs once
        self.assertEqual(ax.register.call_count, 1)
        self.assertEqual(ax.register.call_args[0][0], agent._finalize_task)

    def test_finalize_unregisters_and_writes_wall_clock_once(self):
        agent = _bare_lifecycle_agent()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch(f"{MOD}.atexit") as ax, \
                mock.patch.dict(os.environ, {"RELAY_WALL_OUT": f"{tmp}/wall.json"}):
            agent._begin_task_once()
            agent._task_t0 = time.monotonic() - 2.0
            agent._finalize_task()
            self.assertGreaterEqual(ax.unregister.call_count, 1)
            self.assertEqual(ax.unregister.call_args[0][0], agent._finalize_task)
            doc = json.loads(Path(tmp, "wall.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["phase"], "task")
            self.assertGreaterEqual(doc["wall_s"], 2.0)
            # Second finalize: tolerated, does not rewrite the wall clock.
            Path(tmp, "wall.json").unlink()
            agent._finalize_task()
            self.assertFalse(Path(tmp, "wall.json").exists())

    def test_bound_method_equality_makes_unregister_match(self):
        # atexit.unregister removes entries equal to its argument; bound
        # methods of the same instance must compare equal or the fix is a
        # silent no-op.
        agent = _bare_lifecycle_agent()
        self.assertEqual(agent._finalize_task, agent._finalize_task)


class _StubAgent(MCPAgent):
    def predict(self, observation):  # pragma: no cover — never called
        return "", None


def _agent_with_capture(captured: list) -> _StubAgent:
    agent = _StubAgent(tools=[])
    resp = mock.Mock()
    resp.usage = None
    msg = mock.Mock()
    msg.content = "ok"
    msg.reasoning_content = None
    resp.choices = [mock.Mock(message=msg)]

    def create(**kw):
        captured.append(kw)
        return resp

    client = mock.Mock()
    client.chat.completions.create = create
    agent.openai_client = client
    return agent


class ClaudeParamTests(unittest.TestCase):
    def test_explicit_caller_params_respected(self):
        captured: list = []
        agent = _agent_with_capture(captured)
        out = agent.openai_chat_completions_create(
            model="Claude-Sonnet-4", messages=[], max_tokens=128, temperature=0.0
        )
        self.assertEqual(out, "ok")
        self.assertEqual(captured[0]["max_tokens"], 128)
        self.assertEqual(captured[0]["temperature"], 0.0)

    def test_default_budget_when_caller_omits_max_tokens(self):
        captured: list = []
        agent = _agent_with_capture(captured)
        agent.openai_chat_completions_create(model="claude-opus", messages=[])
        self.assertEqual(captured[0]["max_tokens"], 64000)

    def test_match_is_case_insensitive(self):
        captured: list = []
        agent = _agent_with_capture(captured)
        agent.openai_chat_completions_create(model="CLAUDE-X", messages=[])
        self.assertEqual(captured[0]["max_tokens"], 64000)

    def test_gpt_branch_unchanged(self):
        captured: list = []
        agent = _agent_with_capture(captured)
        agent.openai_chat_completions_create(
            model="gpt-4o", messages=[], max_tokens=100
        )
        self.assertEqual(captured[0]["max_completion_tokens"], 100)
        self.assertNotIn("max_tokens", captured[0])


if __name__ == "__main__":
    unittest.main()
