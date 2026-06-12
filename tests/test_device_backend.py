"""Device-less tests for the DeviceBackend layer.

All adb traffic is mocked at `agents.device.android.subprocess.run`; the
assertions pin the argv each backend method composes (serial injection,
keycode mapping, the AdbKeyboard broadcast) and the pure helpers
(`_xml_to_nodes`, UINode.center, factory dispatch).
"""
from __future__ import annotations

import os
import subprocess
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from agents.device import Key, UINode, set_default_backend
from agents.device.android import AndroidBackend, _xml_to_nodes
from agents.device.factory import _create, current_platform
from agents.device.harmony import HarmonyBackend
from agents.device.ios import IOSBackend


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class AdbArgvTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("agents.device.android.subprocess.run")
        self.run = patcher.start()
        self.addCleanup(patcher.stop)
        self.run.return_value = _cp()

    def _argv(self, call_index: int = 0) -> list[str]:
        return self.run.call_args_list[call_index].args[0]

    def test_serial_injected(self):
        b = AndroidBackend(serial="ABC123")
        self.assertEqual(b.adb_base(), ["adb", "-s", "ABC123"])
        b.tap(10, 20)
        self.assertEqual(
            self._argv(), ["adb", "-s", "ABC123", "shell", "input", "tap", "10", "20"]
        )

    def test_no_serial(self):
        self.assertEqual(AndroidBackend().adb_base(), ["adb"])

    def test_tap_failure_paths(self):
        b = AndroidBackend()
        self.assertTrue(b.tap(1, 2))
        self.run.return_value = _cp(returncode=1, stderr="boom")
        self.assertFalse(b.tap(1, 2))
        self.run.side_effect = subprocess.TimeoutExpired(cmd="adb", timeout=5)
        self.assertFalse(b.tap(1, 2))

    def test_key_mapping(self):
        b = AndroidBackend()
        b.key(Key.BACK)
        self.assertEqual(self._argv()[-2:], ["keyevent", "KEYCODE_BACK"])
        b.key(Key.HOME)
        self.assertEqual(self._argv(1)[-1], "KEYCODE_HOME")
        b.key(Key.ENTER)
        self.assertEqual(self._argv(2)[-1], "KEYCODE_ENTER")

    def test_input_text_is_b64_broadcast(self):
        b = AndroidBackend()
        b.input_text("你好 hi")
        argv = self._argv()
        self.assertIn("ADB_INPUT_B64", argv)
        import base64
        b64 = argv[argv.index("msg") + 1]
        self.assertEqual(base64.b64decode(b64).decode("utf-8"), "你好 hi")

    def test_long_press_is_same_point_swipe(self):
        b = AndroidBackend()
        b.long_press(5, 6)
        self.assertEqual(
            self._argv()[-6:], ["swipe", "5", "6", "5", "6", "1000"]
        )

    def test_screen_size_parse_and_instance_cache(self):
        b = AndroidBackend()
        self.run.return_value = _cp(stdout="Physical size: 1080x2400\n")
        self.assertEqual(b.screen_size(), (1080, 2400))
        self.assertEqual(b.screen_size(), (1080, 2400))
        self.assertEqual(self.run.call_count, 1)  # cached per instance
        b2 = AndroidBackend()
        b2.screen_size()
        self.assertEqual(self.run.call_count, 2)  # new instance re-probes

    def test_screen_size_fallback(self):
        b = AndroidBackend()
        self.run.return_value = _cp(stdout="garbage")
        self.assertEqual(b.screen_size(), (1080, 2400))

    def test_launch_raises_on_no_activities(self):
        b = AndroidBackend()
        self.run.return_value = _cp(stdout="No activities found to run")
        with self.assertRaises(RuntimeError):
            b.launch("com.example.app")

    def test_setup_input_channel_missing_keyboard(self):
        b = AndroidBackend()
        self.run.return_value = _cp(stdout="")  # pm list: not installed
        self.assertFalse(b.setup_input_channel())
        self.assertIs(b._input_channel_ok, False)


class XmlToNodesTests(unittest.TestCase):
    XML = (
        '<hierarchy><node text="发送" content-desc="" resource-id="com.x:id/send"'
        ' class="android.widget.Button" package="com.x"'
        ' bounds="[100,200][300,400]" clickable="true" enabled="true"'
        ' focusable="false" scrollable="false" long-clickable="false">'
        '<node text="" content-desc="zero" resource-id="" class="android.view.View"'
        ' package="com.x" bounds="[5,5][5,5]" clickable="false" enabled="true"'
        ' focusable="false" scrollable="false" long-clickable="false"/>'
        "</node></hierarchy>"
    )

    def test_parse_document_order_and_fields(self):
        nodes = _xml_to_nodes(ET.fromstring(self.XML))
        self.assertEqual(len(nodes), 2)
        n = nodes[0]
        self.assertEqual(
            (n.text, n.resource_id, n.package, n.bounds, n.clickable, n.enabled),
            ("发送", "com.x:id/send", "com.x", (100, 200, 300, 400), True, True),
        )
        self.assertEqual(n.center, (200, 300))
        self.assertEqual(nodes[1].desc, "zero")
        self.assertIsNone(nodes[1].center)  # zero-area rect filtered

    def test_center_without_bounds(self):
        self.assertIsNone(UINode(text="x").center)


class SwipeDownTests(unittest.TestCase):
    def test_geometry_and_ratio_clamp(self):
        b = AndroidBackend()
        with mock.patch.object(b, "screen_size", return_value=(1000, 2000)), \
             mock.patch.object(b, "swipe_gesture") as sg, \
             mock.patch.dict(os.environ, {"RELAY_CAPTURE_SCROLL_RATIO": "0.9"}):
            b.swipe_down()  # 0.9 must clamp to 0.5
        # margin = 400, y_start = 1600, travel = 1000 → y_end clamps to margin
        sg.assert_called_once_with(500, 1600, 500, 600, duration_ms=300, timeout=5.0)


class FactoryTests(unittest.TestCase):
    def tearDown(self):
        set_default_backend(None)

    def test_platform_dispatch(self):
        self.assertIsInstance(_create("android"), AndroidBackend)
        self.assertIsInstance(_create("ios"), IOSBackend)
        self.assertIsInstance(_create("harmonyos"), HarmonyBackend)
        with self.assertRaises(ValueError):
            _create("windows-phone")

    def test_current_platform_normalizes(self):
        with mock.patch.dict(os.environ, {"RELAY_PLATFORM": "Harmony"}):
            self.assertEqual(current_platform(), "harmonyos")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RELAY_PLATFORM", None)
            self.assertEqual(current_platform(), "android")

    def test_stubs_instantiable_but_unusable(self):
        for backend in (IOSBackend(), HarmonyBackend()):
            with self.assertRaises(NotImplementedError):
                backend.screencap()
            with self.assertRaises(NotImplementedError):
                backend.tap(1, 2)


if __name__ == "__main__":
    unittest.main()
