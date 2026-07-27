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
from pathlib import Path
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


class DumpUiTreeTests(unittest.TestCase):
    """Stale-dump defense: `uiautomator dump` can fail with exit code 0 (AOSP
    prints "ERROR: ..." and returns), and the remote path is reused across
    calls — without the rm-first + ERROR check a failed dump would pull back
    the PREVIOUS run's tree and serve it as current."""

    XML = (
        '<hierarchy><node text="ok" content-desc="" resource-id="" class="c"'
        ' package="p" bounds="[0,0][10,10]" clickable="true" enabled="true"'
        ' focusable="false" scrollable="false" long-clickable="false"/></hierarchy>'
    )

    def setUp(self):
        patcher = mock.patch("agents.device.android.subprocess.run")
        self.run = patcher.start()
        self.addCleanup(patcher.stop)

    def _install(self, dump_cp: subprocess.CompletedProcess, *, pull_ok: bool):
        def side_effect(argv, **kwargs):
            if "rm" in argv:
                return _cp()
            if "uiautomator" in argv:
                return dump_cp
            if "pull" in argv:
                if pull_ok:
                    Path(argv[-1]).write_text(self.XML, encoding="utf-8")
                    return _cp()
                return _cp(returncode=1, stderr="remote object does not exist")
            return _cp()

        self.run.side_effect = side_effect

    def test_remote_file_removed_before_dump(self):
        b = AndroidBackend()
        self._install(_cp(stdout="UI hierchary dumped to: /sdcard/x.xml"), pull_ok=True)
        nodes = b.dump_ui_tree()
        self.assertIsNotNone(nodes)
        self.assertEqual(nodes[0].text, "ok")
        argvs = [c.args[0] for c in self.run.call_args_list]
        self.assertEqual(argvs[0][-3:], ["rm", "-f", b._remote_dump_path])
        self.assertIn("uiautomator", argvs[1])  # rm strictly precedes the dump

    def test_rc0_error_dump_returns_none_without_pulling(self):
        b = AndroidBackend()
        self._install(_cp(stdout="ERROR: could not get idle state."), pull_ok=True)
        self.assertIsNone(b.dump_ui_tree())
        for c in self.run.call_args_list:
            self.assertNotIn("pull", c.args[0])  # the old tree is never read

    def test_unrecognized_rc0_failure_surfaces_via_pull(self):
        # A failure wording we don't know: the rm guarantees no stale remote
        # file exists, so it degrades to a pull failure → None, never a tree.
        b = AndroidBackend()
        self._install(_cp(stdout="something odd"), pull_ok=False)
        self.assertIsNone(b.dump_ui_tree())

    def test_nonzero_rc_returns_none(self):
        b = AndroidBackend()
        self._install(_cp(returncode=1, stderr="Killed"), pull_ok=True)
        self.assertIsNone(b.dump_ui_tree())


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


class InputTextFallbackTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("agents.device.android.subprocess.run")
        self.run = patcher.start()
        self.addCleanup(patcher.stop)
        self.run.return_value = _cp()

    def test_broadcast_when_channel_unknown_or_ok(self):
        for state in (None, True):
            b = AndroidBackend()
            b._input_channel_ok = state
            b.input_text("你好")
            self.assertIn("ADB_INPUT_B64", self.run.call_args.args[0])

    def test_ascii_fallback_when_channel_down(self):
        b = AndroidBackend()
        b._input_channel_ok = False
        b.input_text("hello world")
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[-3:], ["input", "text", "hello%sworld"])

    def test_non_ascii_without_channel_raises(self):
        b = AndroidBackend()
        b._input_channel_ok = False
        with self.assertRaises(RuntimeError):
            b.input_text("你好")
        self.run.assert_not_called()

    def test_shell_unsafe_ascii_without_channel_raises(self):
        b = AndroidBackend()
        b._input_channel_ok = False
        with self.assertRaises(RuntimeError):
            b.input_text("a&b;c")  # would be parsed by the remote shell

    def test_glob_char_without_channel_raises(self):
        # `?` is no longer in the safe set: the remote sh globs it against
        # cwd entries (a lone "?" at / matches "/d"), silently corrupting the
        # typed text — loud fail is required instead.
        b = AndroidBackend()
        b._input_channel_ok = False
        with self.assertRaises(RuntimeError):
            b.input_text("ready?")
        self.run.assert_not_called()


class AppIdValidationTests(unittest.TestCase):
    """App ids are spliced into a remote shell command line (adb joins argv
    with spaces; the device sh re-parses) — anything outside the package-name
    alphabet must fail loudly before a single adb call."""

    BAD_IDS = ("a.b;am broadcast", "com.x`reboot`", "$(rm -rf /)", "a b",
               "com.x|id", "")

    def setUp(self):
        patcher = mock.patch("agents.device.android.subprocess.run")
        self.run = patcher.start()
        self.addCleanup(patcher.stop)
        self.run.return_value = _cp(stdout="Events injected: 1")

    def test_launch_rejects_metacharacters(self):
        b = AndroidBackend()
        for app_id in self.BAD_IDS:
            with self.assertRaises(ValueError):
                b.launch(app_id)
        self.run.assert_not_called()

    def test_force_stop_rejects_metacharacters(self):
        b = AndroidBackend()
        for app_id in self.BAD_IDS:
            with self.assertRaises(ValueError):
                b.force_stop(app_id)
        self.run.assert_not_called()

    def test_cold_launch_rejects_before_any_device_call(self):
        b = AndroidBackend()
        with self.assertRaises(ValueError):
            b.cold_launch("com.x;rm -rf /sdcard")
        self.run.assert_not_called()

    def test_valid_package_ids_pass(self):
        b = AndroidBackend()
        for app_id in ("com.aliyun.tongyi", "com.autonavi.minimap", "a_b.C1"):
            b.launch(app_id)  # must not raise
        self.assertEqual(self.run.call_count, 3)


class StartRecordingSerialTests(unittest.TestCase):
    def test_instance_serial_reaches_the_recorder(self):
        # start_recording must pass THIS instance's adb prefix — the
        # recorder's default resolves the factory singleton, which is the
        # wrong device when several backends coexist (device-pool contract
        # in the module docstring).
        b = AndroidBackend(serial="DEVICE-B")
        with mock.patch("agents.runtime._recorder.start") as start:
            b.start_recording(Path("/tmp/unused"))
        start.assert_called_once()
        self.assertEqual(start.call_args.kwargs["adb_base"], ["adb", "-s", "DEVICE-B"])


class VendorProfileTests(unittest.TestCase):
    def setUp(self):
        from agents.device import vendor_profiles as vp
        self.vp = vp
        self.addCleanup(vp.permission_packages.cache_clear)
        self.addCleanup(vp.allow_labels.cache_clear)
        vp.permission_packages.cache_clear()
        vp.allow_labels.cache_clear()

    def test_defaults_without_env(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RELAY_VENDOR_PROFILE", None)
            self.assertEqual(self.vp.permission_packages(), self.vp.PERMISSION_PACKAGES)
            self.assertEqual(self.vp.allow_labels(), self.vp.ALLOW_LABELS)

    def test_overlay_merges_and_prepends(self):
        import json
        import tempfile
        overlay = {"permission_packages": ["com.oem.grantor"],
                   "allow_labels": ["总是同意", "允许"]}  # "允许" already a default
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(overlay, f)
            path = f.name
        self.addCleanup(os.unlink, path)
        with mock.patch.dict(os.environ, {"RELAY_VENDOR_PROFILE": path}):
            pkgs = self.vp.permission_packages()
            labels = self.vp.allow_labels()
        self.assertIn("com.oem.grantor", pkgs)
        self.assertEqual(labels[0], "总是同意")          # overlay outranks defaults
        self.assertEqual(labels.count("允许"), 1)        # dedup against defaults

    def test_unreadable_overlay_falls_back_to_defaults(self):
        with mock.patch.dict(os.environ, {"RELAY_VENDOR_PROFILE": "/no/such/file.json"}):
            self.assertEqual(self.vp.permission_packages(), self.vp.PERMISSION_PACKAGES)


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
