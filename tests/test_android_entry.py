"""Host-side tests for the Android entrypoint helpers (relay_android/entry.py).

entry.py is a Chaquopy module — it imports `java` at module scope — so it is
loaded here from its file path under a stubbed `java.jclass` returning a fake
DeviceBridge. Only the Chaquopy-independent pieces are pinned:

- `_cfg_int`: SettingsActivity writes NUM_KEYS as strings, "" when unset —
  blank/None/junk must keep the default instead of int("") crashing run_single.
- `_install_env` / `_apply_env`: "blank field keeps the runtime default" must
  hold across repeated installs in one long-lived process — a key set on a
  previous run is restored to its pre-override value (usually: deleted) when
  its Settings field is cleared.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

_ENTRY_PATH = (
    Path(__file__).resolve().parents[1]
    / "android" / "app" / "src" / "main" / "python" / "relay_android" / "entry.py"
)


def _load_entry(files_dir: str):
    """Load entry.py as a fresh module (clean module-level env snapshot)
    with `java.jclass` stubbed to a fake DeviceBridge."""
    bridge = types.SimpleNamespace(appFilesDir=lambda: files_dir)
    java_mod = types.ModuleType("java")
    java_mod.jclass = lambda name: bridge
    old_java = sys.modules.get("java")
    sys.modules["java"] = java_mod
    try:
        spec = importlib.util.spec_from_file_location(
            "_android_entry_under_test", _ENTRY_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if old_java is None:
            sys.modules.pop("java", None)
        else:
            sys.modules["java"] = old_java


class _EntryTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.entry = _load_entry(self.tmp.name)
        # _install_env mutates os.environ (paths + managed keys); restore all.
        self._env = dict(os.environ)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)


class CfgIntTest(_EntryTestBase):
    def test_blank_and_none_keep_default(self):
        # Regression: cfg["max_step"] arrives as "" from Settings defaults;
        # int("") used to crash run_single with a misleading error.
        self.assertEqual(self.entry._cfg_int("", -1), -1)
        self.assertEqual(self.entry._cfg_int("   ", -1), -1)
        self.assertEqual(self.entry._cfg_int(None, -1), -1)

    def test_numeric_values_parse(self):
        self.assertEqual(self.entry._cfg_int("12", -1), 12)
        self.assertEqual(self.entry._cfg_int(" 8 ", -1), 8)
        self.assertEqual(self.entry._cfg_int(7, -1), 7)
        self.assertEqual(self.entry._cfg_int("-1", 25), -1)

    def test_junk_keeps_default(self):
        self.assertEqual(self.entry._cfg_int("abc", -1), -1)


class InstallEnvRestoreTest(_EntryTestBase):
    def test_set_then_blank_deletes_key(self):
        os.environ.pop("RELAY_STEP_WAIT", None)
        self.entry._install_env({"RELAY_STEP_WAIT": "5"})
        self.assertEqual(os.environ["RELAY_STEP_WAIT"], "5")
        self.entry._install_env({"RELAY_STEP_WAIT": ""})
        self.assertNotIn("RELAY_STEP_WAIT", os.environ)

    def test_set_then_absent_restores_previous_value(self):
        os.environ["LLM_MODEL"] = "qwen"
        self.entry._install_env({"LLM_MODEL": "other"})
        self.assertEqual(os.environ["LLM_MODEL"], "other")
        self.entry._install_env({})
        self.assertEqual(os.environ["LLM_MODEL"], "qwen")

    def test_untouched_keys_are_left_alone(self):
        os.environ["LLM_BASE_URL"] = "http://pre-existing"
        self.entry._install_env({})
        self.assertEqual(os.environ["LLM_BASE_URL"], "http://pre-existing")

    def test_android_layout_env_still_installed(self):
        self.entry._install_env({})
        files = Path(self.tmp.name)
        self.assertEqual(
            os.environ["RELAY_MANIFESTS"], str(files / "relay" / "manifests")
        )
        self.assertEqual(os.environ["RELAY_LEG_EXECUTOR"], "inprocess")
        self.assertEqual(os.environ["RELAY_MW_FALLBACK"], "0")


if __name__ == "__main__":
    unittest.main()
