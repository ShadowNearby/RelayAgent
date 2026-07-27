"""mw_llm_probe._flush: one unserializable value must not drop the per-call
log, and a flush failure must be surfaced on stderr (never silent)."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from agents.llm import mw_llm_probe


def _call(**overrides) -> dict:
    base = {
        "index": 0,
        "purpose": "GeneralAgent",
        "model": "m",
        "elapsed_s": 0.1,
        "ok": True,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cached_tokens": 1,
        "total_tokens": 15,
        "messages": [{"role": "user", "content": "hi"}],
        "response": "ok",
    }
    base.update(overrides)
    return base


class FlushTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.out = Path(self.dir.name) / "calls.json"

    def test_flush_writes_totals_and_calls(self):
        calls = [_call(), _call(index=1, prompt_tokens=20, completion_tokens=10)]
        with mock.patch.object(mw_llm_probe, "_CALLS", calls):
            mw_llm_probe._flush(self.out)
        doc = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(doc["n_calls"], 2)
        self.assertEqual(doc["total"]["prompt_tokens"], 30)
        self.assertEqual(doc["total"]["total_tokens"], 45)

    def test_unserializable_value_degrades_to_string_not_data_loss(self):
        # MW-side message payloads aren't under our control; _sanitize_messages
        # passes non-dict elements through untouched. One bad value used to
        # TypeError json.dumps and silently drop the WHOLE file.
        calls = [_call(messages=[object()]), _call(index=1)]
        with mock.patch.object(mw_llm_probe, "_CALLS", calls):
            mw_llm_probe._flush(self.out)
        doc = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(doc["n_calls"], 2)  # both calls survived
        self.assertIsInstance(doc["llm_calls"][0]["messages"][0], str)
        self.assertEqual(doc["llm_calls"][1]["response"], "ok")

    def test_flush_failure_warns_on_stderr_and_never_raises(self):
        blocker = Path(self.dir.name) / "blocker"
        blocker.write_text("file, not a dir", encoding="utf-8")
        out = blocker / "calls.json"  # parent mkdir will fail
        with mock.patch.object(mw_llm_probe, "_CALLS", [_call()]):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                mw_llm_probe._flush(out)  # must not raise (atexit hook)
        self.assertIn("[mw_llm_probe]", stderr.getvalue())
        self.assertIn("calls.json", stderr.getvalue())


class JsonDefaultTests(unittest.TestCase):
    def test_stringifies_and_truncates(self):
        self.assertEqual(mw_llm_probe._json_default(3.5), "3.5")
        self.assertEqual(len(mw_llm_probe._json_default("x" * 5000)), 2000)

    def test_placeholder_when_str_fails(self):
        class Unprintable:
            def __str__(self):
                raise RuntimeError("nope")

        self.assertIn("unserializable", mw_llm_probe._json_default(Unprintable()))


if __name__ == "__main__":
    unittest.main()
