"""AndroidBackend — RelayAgent device primitives over the Kotlin DeviceBridge.

Each method mirrors one host-adb primitive (see agents/_adb.py and the
NativeEnv dispatch in agents/native_runtime.py); the Kotlin side implements
them with AccessibilityService gestures, MediaProjection capture and the
uiautomator-format A11yXmlSerializer.

Injection: `install()` plugs this backend into the runtime's device seam
(`agents.device` — the DeviceBackend abstraction being landed on the
device-backend branch). Until that seam exists in the synced agents/ tree,
install() fails fast with an actionable error rather than letting the
runtime silently fall through to subprocess adb (which doesn't exist on
the phone).

Known semantic drift vs. host (accepted, see plan §risks): no real
force-stop without shell — cold launch approximates it with a CLEAR_TASK
relaunch, so target-app in-memory state may survive across legs.
"""
from __future__ import annotations

import io
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from java import jclass  # Chaquopy
from loguru import logger

Bridge = jclass("com.relayagent.app.DeviceBridge")


class AndroidBackend:
    # -- observation --------------------------------------------------------
    def screencap(self, timeout: float = 5.0):
        from PIL import Image

        png = Bridge.screencapPng()
        if png is None:
            logger.warning("screencap: no frame (projection down and a11y capture failed)")
            return None
        try:
            return Image.open(io.BytesIO(bytes(png))).convert("RGB")
        except Exception as e:
            logger.warning(f"screencap decode failed: {e}")
            return None

    def screen_size(self) -> tuple[int, int]:
        w, h = Bridge.screenSize()
        return int(w), int(h)

    def ui_dump_xml(
        self, dump_timeout: float = 8.0, pull_timeout: float = 5.0
    ) -> "ET.Element | None":
        xml = Bridge.uiDumpXml()
        if not xml:
            logger.info("ui dump unavailable (a11y service down?)")
            return None
        try:
            return ET.fromstring(xml)
        except ET.ParseError as e:
            logger.info(f"ui dump XML parse error: {e}")
            return None

    def foreground_package(self, timeout: float = 3.0) -> str | None:
        pkg = Bridge.foregroundPackage()
        return str(pkg) if pkg else None

    # -- gestures / keys -----------------------------------------------------
    def tap(self, x: int, y: int) -> None:
        if not Bridge.tap(int(x), int(y)):
            logger.warning(f"tap ({x},{y}) failed")

    def long_press(self, x: int, y: int, duration_ms: int = 1000) -> None:
        if not Bridge.longPress(int(x), int(y), int(duration_ms)):
            logger.warning(f"long_press ({x},{y}) failed")

    def swipe(self, x0: int, y0: int, x1: int, y1: int, duration_ms: int = 400) -> None:
        if not Bridge.swipe(int(x0), int(y0), int(x1), int(y1), int(duration_ms)):
            logger.warning(f"swipe ({x0},{y0})->({x1},{y1}) failed")

    def keyevent(self, code: str) -> None:
        if not Bridge.keyevent(code):
            logger.warning(f"keyevent {code} failed")

    # -- text input ----------------------------------------------------------
    def input_text(self, text: str) -> None:
        if not Bridge.inputText(text):
            logger.warning("input_text failed (no focused editable / paste rejected)")

    def prepare_text_input(self) -> bool:
        # No AdbKeyboard on-device: input goes through a11y SET_TEXT/paste.
        return True

    def restore_text_input(self) -> None:
        pass

    # -- app lifecycle -------------------------------------------------------
    def open_app(self, package: str) -> None:
        if not Bridge.launchApp(package):
            raise RuntimeError(f"Failed to launch {package} (no launch intent?)")

    def force_stop(self, package: str, timeout: float = 10.0) -> None:
        # Logged drift: CLEAR_TASK relaunch in open_app is the approximation.
        Bridge.forceStopApprox(package)

    def kill_all_apps(self, timeout: float = 25.0) -> list[str]:
        logger.info("kill_all_apps: unavailable without shell; skipped")
        return []

    def start_recording(self, out_dir: Path, basename: str = "recording"):
        logger.info("recording: not implemented on-device yet (Phase 4)")
        return None


def install() -> AndroidBackend:
    """Inject AndroidBackend as the runtime's device backend. Must be called
    before any agents.* module performs device I/O."""
    backend = AndroidBackend()
    try:
        from agents.device import set_backend  # the DeviceBackend seam
    except ImportError as e:
        raise RuntimeError(
            "agents.device backend seam not present in this build — sync a "
            "repo state where the DeviceBackend abstraction (P0.1) has landed"
        ) from e
    set_backend(backend)
    logger.info("AndroidBackend installed as the device backend")
    return backend
