"""Unit tests for the user memory layer (roadmap P3).

Pins: store load/validation degradation, the RELAY_PROFILE switch, choice
memory (M2③), traj redaction placeholders (M4), and the propose-then-ask
memory contract (M3) — all device-less.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from agents.flow import user_profile
from agents.flow.user_profile import (
    UserProfile,
    load_profile,
    propose_memory,
    redact_obj,
)


class ProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp())
        os.environ["RELAY_PROFILE_ROOT"] = str(self._tmp)
        os.environ.pop("RELAY_PROFILE", None)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        for k in ("RELAY_PROFILE_ROOT", "RELAY_PROFILE", "RELAY_TRAJ_REDACT"):
            os.environ.pop(k, None)

    def _write(self, data: dict) -> None:
        (self._tmp / "profile.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
        )

    def test_missing_file_is_none(self) -> None:
        self.assertIsNone(load_profile())

    def test_switch_off_is_none_even_with_file(self) -> None:
        self._write({"addresses": {"home": "X路1号"}})
        os.environ["RELAY_PROFILE"] = "0"
        self.assertIsNone(load_profile())

    def test_loads_and_summarizes(self) -> None:
        self._write({
            "addresses": {"home": "X路1号"},
            "preferences": {"milk_tea": "去冰三分糖"},
        })
        p = load_profile()
        self.assertIsNotNone(p)
        self.assertFalse(p.is_empty())
        self.assertIn("home=X路1号", p.summary())
        self.assertIn("milk_tea=去冰三分糖", p.summary())
        # last_choices never rides prompt injection
        p.remember_choice("哪家店?", "蜜雪冰城")
        self.assertNotIn("蜜雪冰城", p.summary())

    def test_malformed_section_degrades_to_empty(self) -> None:
        self._write({"addresses": ["not", "a", "map"], "contacts": {"a": "b"}})
        p = load_profile()
        self.assertEqual(p.section("addresses"), {})
        self.assertEqual(p.section("contacts"), {"a": "b"})

    def test_malformed_section_survives_an_unrelated_save(self) -> None:
        # A degraded READ must never become a destructive WRITE-BACK: a hand-
        # edited section that slightly violates the schema (a nested map here)
        # is ignored in memory, but a later save (choice memory writes on every
        # select_from pick) must leave the user's data on disk intact.
        self._write({
            "preferences": {"milk_tea": {"ice": "less"}},  # nested → malformed
            "addresses": {"home": "X路1号"},
        })
        p = load_profile()
        self.assertEqual(p.section("preferences"), {})  # ignored on read
        p.remember_choice("选哪个门店?", "人民广场店")  # unrelated write → save()
        on_disk = yaml.safe_load(
            (self._tmp / "profile.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(on_disk["preferences"], {"milk_tea": {"ice": "less"}})
        self.assertEqual(on_disk["addresses"], {"home": "X路1号"})
        self.assertEqual(on_disk["last_choices"]["选哪个门店?"], "人民广场店")

    def test_writes_into_a_malformed_section_are_refused(self) -> None:
        # Writing INTO the malformed section would still clobber it (the
        # in-memory view is empty), so that write is skipped instead.
        self._write({"preferences": {"milk_tea": {"ice": "less"}}})
        p = load_profile()
        p.add_preference("coffee", "拿铁")
        on_disk = yaml.safe_load(
            (self._tmp / "profile.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(on_disk["preferences"], {"milk_tea": {"ice": "less"}})

    def test_choice_memory_roundtrip_persists(self) -> None:
        self._write({})
        load_profile().remember_choice("选哪个门店?", "人民广场店")
        self.assertEqual(load_profile().get_choice("选哪个门店?"), "人民广场店")

    def test_add_preference_creates_store(self) -> None:
        p = UserProfile({"version": 1}, self._tmp / "profile.yaml")
        p.add_preference("milk_tea", "去冰")
        self.assertEqual(load_profile().section("preferences"), {"milk_tea": "去冰"})


class RedactTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp())
        os.environ["RELAY_PROFILE_ROOT"] = str(self._tmp)
        (self._tmp / "profile.yaml").write_text(
            yaml.safe_dump({"addresses": {"home": "幸福路99号"}}, allow_unicode=True),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        for k in ("RELAY_PROFILE_ROOT", "RELAY_TRAJ_REDACT"):
            os.environ.pop(k, None)

    def test_off_by_default_is_identity(self) -> None:
        obj = {"prompt": "导航到幸福路99号"}
        self.assertIs(redact_obj(obj), obj)

    def test_replaces_values_with_placeholders(self) -> None:
        os.environ["RELAY_TRAJ_REDACT"] = "1"
        obj = {"calls": [{"content": "导航到幸福路99号", "n": 3}], "note": None}
        red = redact_obj(obj)
        self.assertEqual(red["calls"][0]["content"], "导航到<profile:addresses.home>")
        self.assertEqual(red["calls"][0]["n"], 3)  # non-strings untouched
        # original object not mutated
        self.assertIn("幸福路99号", obj["calls"][0]["content"])


class ProposeMemoryTests(unittest.TestCase):
    def _llm(self, reply: str) -> MagicMock:
        llm = MagicMock()
        llm.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content=reply))
        ]
        return llm

    def test_save_proposal_parsed(self) -> None:
        llm = self._llm('{"save": true, "key": "milk_tea", "value": "去冰"}')
        self.assertEqual(propose_memory(llm, "m", "点奶茶去冰", {}), ("milk_tea", "去冰"))

    def test_no_save_is_none(self) -> None:
        self.assertIsNone(propose_memory(self._llm('{"save": false}'), "m", "查天气", {}))

    def test_garbage_is_none_never_raises(self) -> None:
        self.assertIsNone(propose_memory(self._llm("not json"), "m", "x", {}))


if __name__ == "__main__":
    unittest.main()
