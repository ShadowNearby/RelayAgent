"""Device-less tests for the HarmonyOS (hdc/uitest) backend.

All hdc traffic is mocked at `agents.device.harmony.subprocess.run`; the
assertions pin the argv each method composes (target -t injection, the uiInput
verbs, keyEvent mapping) and the pure helpers (`_layout_to_nodes`, screen-size
caching/fallback, factory dispatch with RELAY_HARMONY_SERIAL).

Mirrors tests/test_device_backend.py for the Android backend.
"""
from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

from agents.device import Key, UINode, set_default_backend
from agents.device.factory import _create
from agents.device.harmony import HarmonyBackend, _layout_to_nodes


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class HdcArgvTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("agents.device.harmony.subprocess.run")
        self.run = patcher.start()
        self.addCleanup(patcher.stop)
        self.run.return_value = _cp()

    def _argv(self, call_index: int = 0) -> list[str]:
        return self.run.call_args_list[call_index].args[0]

    def test_target_injected(self):
        b = HarmonyBackend(serial="KEY9")
        self.assertEqual(b.hdc_base(), ["hdc", "-t", "KEY9"])
        b.tap(10, 20)
        self.assertEqual(
            self._argv(),
            ["hdc", "-t", "KEY9", "shell", "uitest", "uiInput", "click", "10", "20"],
        )

    def test_no_serial(self):
        self.assertEqual(HarmonyBackend().hdc_base(), ["hdc"])

    def test_tap_failure_paths(self):
        b = HarmonyBackend()
        self.assertTrue(b.tap(1, 2))
        self.run.return_value = _cp(returncode=1, stderr="boom")
        self.assertFalse(b.tap(1, 2))
        self.run.side_effect = subprocess.TimeoutExpired(cmd="hdc", timeout=5)
        self.assertFalse(b.tap(1, 2))

    def test_key_mapping(self):
        b = HarmonyBackend()
        b.key(Key.BACK)
        self.assertEqual(self._argv()[-2:], ["keyEvent", "Back"])
        b.key(Key.HOME)
        self.assertEqual(self._argv(1)[-1], "Home")
        b.key(Key.ENTER)
        self.assertEqual(self._argv(2)[-1], "2054")

    def test_input_text_direct_uiinput(self):
        b = HarmonyBackend()
        b.input_text("你好 hi")
        argv = self._argv()
        self.assertEqual(argv[-4:], ["uitest", "uiInput", "inputText", "你好 hi"])

    def test_long_press_is_long_click(self):
        b = HarmonyBackend()
        b.long_press(5, 6)
        self.assertEqual(self._argv()[-4:], ["uiInput", "longClick", "5", "6"])

    def test_swipe_argv(self):
        b = HarmonyBackend()
        b.swipe_gesture(1, 2, 3, 4)
        self.assertEqual(self._argv()[-6:], ["uiInput", "swipe", "1", "2", "3", "4"])

    def test_screen_size_parse_and_instance_cache(self):
        b = HarmonyBackend()
        self.run.return_value = _cp(stdout="physical screen: 1080x2400\n")
        self.assertEqual(b.screen_size(), (1080, 2400))
        self.assertEqual(b.screen_size(), (1080, 2400))
        self.assertEqual(self.run.call_count, 1)  # cached per instance
        HarmonyBackend().screen_size()
        self.assertEqual(self.run.call_count, 2)  # new instance re-probes

    def test_screen_size_fallback(self):
        b = HarmonyBackend()
        self.run.return_value = _cp(stdout="no resolution here")
        self.assertEqual(b.screen_size(), (1080, 2400))

    def test_launch_splits_bundle_ability(self):
        b = HarmonyBackend()
        b.launch("com.example.app/EntryAbility")
        self.assertEqual(
            self._argv(),
            ["hdc", "shell", "aa", "start", "-b", "com.example.app",
             "-a", "EntryAbility"],
        )

    def test_launch_bundle_only(self):
        b = HarmonyBackend()
        b.launch("com.example.app")
        self.assertEqual(self._argv()[-3:], ["start", "-b", "com.example.app"])

    def test_launch_raises_on_error_output(self):
        b = HarmonyBackend()
        self.run.return_value = _cp(stdout="error: ability not found", returncode=1)
        with self.assertRaises(RuntimeError):
            b.launch("com.example.app")

    def test_force_stop_drops_ability_suffix(self):
        b = HarmonyBackend()
        b.force_stop("com.example.app/EntryAbility")
        self.assertEqual(self._argv()[-3:], ["aa", "force-stop", "com.example.app"])

    def test_setup_input_channel_is_noop_true(self):
        b = HarmonyBackend()
        self.assertTrue(b.setup_input_channel())
        self.assertIs(b._input_channel_ok, True)
        self.run.assert_not_called()  # no IME swap on HarmonyOS

    def test_foreground_app_parses_bundle(self):
        b = HarmonyBackend()
        self.run.return_value = _cp(stdout="  bundle name [com.aliyun.tongyi]\n")
        self.assertEqual(b.foreground_app(), "com.aliyun.tongyi")


class LayoutToNodesTests(unittest.TestCase):
    TREE = {
        "attributes": {"type": "root", "bundleName": "com.x"},
        "children": [
            {
                "attributes": {
                    "text": "发送", "description": "", "id": "com.x:id/send",
                    "type": "Button", "bundleName": "com.x",
                    "bounds": "[100,200][300,400]", "clickable": "true",
                    "enabled": "true", "focusable": "false",
                    "scrollable": "false", "longClickable": "false",
                },
                "children": [
                    {
                        "attributes": {
                            "text": "", "description": "zero", "id": "",
                            "type": "View", "bundleName": "com.x",
                            "bounds": "[5,5][5,5]", "clickable": False,
                            "enabled": True,
                        },
                        "children": [],
                    }
                ],
            }
        ],
    }

    def test_parse_document_order_and_fields(self):
        nodes = _layout_to_nodes(self.TREE)
        # root + button + inner view = 3 nodes, document order
        self.assertEqual(len(nodes), 3)
        btn = nodes[1]
        self.assertEqual(
            (btn.text, btn.resource_id, btn.package, btn.bounds, btn.clickable, btn.enabled),
            ("发送", "com.x:id/send", "com.x", (100, 200, 300, 400), True, True),
        )
        self.assertEqual(btn.center, (200, 300))
        inner = nodes[2]
        self.assertEqual(inner.desc, "zero")
        self.assertFalse(inner.clickable)        # bool flag honored
        self.assertIsNone(inner.center)          # zero-area rect filtered

    def test_top_level_array_forest(self):
        nodes = _layout_to_nodes([self.TREE, self.TREE])
        self.assertEqual(len(nodes), 6)

    def test_center_without_bounds(self):
        self.assertIsNone(UINode(text="x").center)


class FactoryTests(unittest.TestCase):
    def tearDown(self):
        set_default_backend(None)

    def test_harmony_serial_env_passed(self):
        with mock.patch.dict(os.environ, {"RELAY_HARMONY_SERIAL": "KEY9"}):
            backend = _create("harmonyos")
        self.assertIsInstance(backend, HarmonyBackend)
        self.assertEqual(backend.serial, "KEY9")

    def test_harmony_no_serial(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RELAY_HARMONY_SERIAL", None)
            backend = _create("harmonyos")
        self.assertIsNone(backend.serial)


if __name__ == "__main__":
    unittest.main()
