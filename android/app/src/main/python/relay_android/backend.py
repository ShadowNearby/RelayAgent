"""OnDeviceAndroidBackend — DeviceBackend over the Kotlin DeviceBridge.

Implements the `agents.device.DeviceBackend` seam without adb: the Kotlin
side provides AccessibilityService gestures, MediaProjection capture and the
uiautomator-format A11yXmlSerializer, so `dump_ui_tree` reuses the same
XML→UINode normalizer as the host adb backend.

Injection: `install()` registers this backend as the process default via
`agents.device.set_default_backend`. Must run before any agents.* module
performs device I/O — otherwise the factory would construct the adb-backed
AndroidBackend, and subprocess adb doesn't exist on the phone.

Known semantic drift vs. host (accepted, see plan §risks):
- no real force-stop without shell — cold launch approximates it with a
  CLEAR_TASK relaunch, so target-app in-memory state may survive across legs.
- recording is a Phase 4 item; `start_recording` returns None.
"""
from __future__ import annotations

import io
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from java import jclass  # Chaquopy
from loguru import logger

from agents.device import DeviceBackend, Key, UINode
# Same uiautomator XML dialect on both sides — share the normalizer and the
# logical-key mapping with the host adb backend.
from agents.device.android import _KEYCODES, AndroidBackend, _xml_to_nodes

Bridge = jclass("com.relayagent.app.DeviceBridge")


class OnDeviceAndroidBackend(DeviceBackend):
    platform = "android"

    # -- observation --------------------------------------------------------
    def screencap(self, timeout: float = 5.0):
        from PIL import Image

        # JPEG (q85) from the Kotlin side — a PNG round-trip cost hundreds of
        # ms per frame for no consumer benefit; Image.open sniffs the format.
        data = Bridge.screencapJpeg()
        if data is None:
            logger.warning("screencap: no frame (projection down and a11y capture failed)")
            return None
        try:
            return Image.open(io.BytesIO(bytes(data))).convert("RGB")
        except Exception as e:
            logger.warning(f"screencap decode failed: {e}")
            return None

    def screen_size(self, timeout: float = 5.0) -> tuple[int, int]:
        w, h = Bridge.screenSize()
        return int(w), int(h)

    def dump_ui_tree(
        self, *, dump_timeout: float = 8.0, pull_timeout: float = 5.0
    ) -> list[UINode] | None:
        xml = Bridge.uiDumpXml()
        if not xml:
            logger.info("ui dump unavailable (a11y service down?)")
            return None
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            logger.info(f"ui dump XML parse error: {e}")
            return None
        return _xml_to_nodes(root)

    def foreground_app(self, *, timeout: float = 5.0) -> str | None:
        pkg = Bridge.foregroundPackage()
        return str(pkg) if pkg else None

    def wait_settled(self, budget: float, quiet: float | None = None) -> bool:
        """Frame-arrival settle detection off the MediaProjection pipeline —
        the on-device analogue of the host's scrcpy version (P2-S2): the
        VirtualDisplay surface only receives buffers when the screen CHANGES,
        so "frameSeq unchanged for a whole quiet window" means settled. Worst
        case (continuous animation) spends exactly `budget`, identical to the
        fixed sleep it replaces. Returns False (→ caller sleeps fixed) when
        the projection is down or `RELAY_SETTLE_DETECT=0`.

        No blocking primitive crosses the Chaquopy bridge, so this polls
        `Bridge.captureFrameSeq()` (~a cheap static field read) instead of
        waiting on a condition variable.
        """
        if os.getenv("RELAY_SETTLE_DETECT", "1") != "1":
            return False
        seq = int(Bridge.captureFrameSeq())
        if seq < 0:
            return False  # projection down → keep the fixed-sleep behavior
        if quiet is None:
            quiet = float(os.getenv("RELAY_SETTLE_QUIET", "0.2"))
        deadline = time.monotonic() + budget
        last_change = time.monotonic()
        poll = 0.03
        while True:
            now = time.monotonic()
            if now >= deadline:
                return True  # budget spent — proceed, like the old fixed sleep
            if now - last_change >= quiet:
                return True  # no new frame for a whole quiet window → settled
            time.sleep(min(poll, deadline - now))
            new_seq = int(Bridge.captureFrameSeq())
            if new_seq != seq and new_seq >= 0:
                seq = new_seq
                last_change = time.monotonic()

    # -- gestures / input -----------------------------------------------------
    def tap(self, x: int, y: int, *, timeout: float = 5.0) -> bool:
        if not Bridge.tap(int(x), int(y)):
            logger.warning(f"tap ({x},{y}) failed")
            return False
        return True

    def long_press(self, x: int, y: int, *, duration_ms: int = 1000) -> None:
        if not Bridge.longPress(int(x), int(y), int(duration_ms)):
            logger.warning(f"long_press ({x},{y}) failed")

    def swipe_gesture(
        self, x0: int, y0: int, x1: int, y1: int,
        *, duration_ms: int = 400, timeout: float = 10.0,
    ) -> None:
        if not Bridge.swipe(int(x0), int(y0), int(x1), int(y1), int(duration_ms)):
            logger.warning(f"swipe ({x0},{y0})->({x1},{y1}) failed")

    def key(self, key: Key) -> None:
        if not Bridge.keyevent(_KEYCODES[key]):
            logger.warning(f"keyevent {key} failed")

    def input_text(self, text: str) -> None:
        if not Bridge.inputText(text):
            logger.warning("input_text failed (no focused editable / paste rejected)")

    # -- app lifecycle --------------------------------------------------------
    def launch(self, app_id: str, *, timeout: float = 10.0) -> None:
        if not Bridge.launchApp(app_id):
            raise RuntimeError(f"Failed to launch {app_id} (no launch intent?)")

    def cold_launch(
        self, app_id: str, *, settle_seconds: float = 1.0, timeout: float = 10.0
    ) -> None:
        self.force_stop(app_id, timeout=timeout)
        self.launch(app_id, timeout=timeout)
        time.sleep(settle_seconds)

    def force_stop(self, app_id: str, *, timeout: float = 10.0) -> None:
        # Logged drift: CLEAR_TASK relaunch is the no-shell approximation.
        Bridge.forceStopApprox(app_id)

    def kill_all_apps(self, *, timeout: float = 25.0) -> list[str]:
        logger.info("kill_all_apps: unavailable without shell; skipped")
        return []

    # -- input channel --------------------------------------------------------
    def setup_input_channel(self) -> bool:
        # No AdbKeyboard on-device: input goes through a11y SET_TEXT/paste.
        return True

    def teardown_input_channel(self) -> None:
        pass

    # -- permission popups -----------------------------------------------------
    # The host implementation only touches the DeviceBackend interface
    # (foreground_app / dump_ui_tree / tap) + the vendor tables, so it works
    # verbatim over the Kotlin bridge. Same opt-out (RELAY_DISMISS_PERMISSIONS,
    # exposed in the Settings screen) and same never-Deny policy.
    dismiss_permission_popup = AndroidBackend.dismiss_permission_popup

    # -- recording ------------------------------------------------------------
    def start_recording(self, out_dir: Path, *, basename: str = "recording"):
        logger.info("recording: not implemented on-device yet (Phase 4)")
        return None


def install() -> OnDeviceAndroidBackend:
    """Inject the on-device backend as the process default. Must be called
    before any agents.* module performs device I/O."""
    backend = OnDeviceAndroidBackend()
    try:
        from agents.device import set_default_backend
    except ImportError as e:
        raise RuntimeError(
            "agents.device backend seam not present in this build — sync a "
            "repo state where the DeviceBackend abstraction has landed"
        ) from e
    set_default_backend(backend)
    logger.info("OnDeviceAndroidBackend installed as the device backend")
    return backend
