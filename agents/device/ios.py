"""IOSBackend — interface skeleton for iOS via WebDriverAgent (WDA).

NOT IMPLEMENTED yet: every method raises NotImplementedError. The class is
importable and instantiable so the factory can dispatch on RELAY_PLATFORM=ios
and fail with a clear, single-line error at the first device call instead of
deep inside a traceback. Full capability mapping: docs/device_backends.md.

Planned wiring (requires a Mac + signed WDA on the device; the WDA HTTP
endpoint replaces every adb subprocess):

  screencap            GET  /screenshot                (base64 PNG)
  screen_size          GET  /window/size
  dump_ui_tree         GET  /source?format=json        → UINode list
                       (label → desc, identifier/name → resource_id,
                        rect → bounds, type → class_name)
  foreground_app       GET  /wda/activeAppInfo         (bundleId)
  tap / long_press     POST /wda/touch/perform
  swipe_gesture        POST /wda/dragfromtoforduration
  key BACK             edge swipe (iOS has no system back key)
  key HOME             POST /wda/homescreen
  key ENTER            POST /wda/keys  ("\\n")
  input_text           POST /wda/keys  (no IME dependency — CJK works)
  launch / cold_launch POST /session {bundleId} / terminate + launch
  force_stop           POST /wda/apps/terminate
  kill_all_apps        no public enumeration — terminate known app_ids only
  setup_input_channel  no-op (WDA types directly)
  start_recording      WDA mjpeg port; real-device file recording is a GAP
                       (QuickTime/ReplayKit routes are out of scope here)
  permission popups    springboard alerts: GET /alert/text + POST /alert/accept
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.device.base import DeviceBackend, Key, RecordingHandle, UINode

_MSG = (
    "IOSBackend is an interface skeleton — not implemented yet. "
    "See docs/device_backends.md for the WebDriverAgent mapping."
)


class IOSBackend(DeviceBackend):
    platform = "ios"

    def __init__(self, wda_url: str | None = None) -> None:
        # Constructor must not raise: the factory instantiates eagerly and
        # tests assert dispatchability; only *use* fails.
        self.wda_url = wda_url

    def screencap(self, timeout: float = 5.0) -> Any | None:
        raise NotImplementedError(_MSG)

    def screen_size(self, timeout: float = 5.0) -> tuple[int, int]:
        raise NotImplementedError(_MSG)

    def dump_ui_tree(
        self, *, dump_timeout: float = 8.0, pull_timeout: float = 5.0
    ) -> list[UINode] | None:
        raise NotImplementedError(_MSG)

    def foreground_app(self, *, timeout: float = 5.0) -> str | None:
        raise NotImplementedError(_MSG)

    def tap(self, x: int, y: int, *, timeout: float = 5.0) -> bool:
        raise NotImplementedError(_MSG)

    def long_press(self, x: int, y: int, *, duration_ms: int = 1000) -> None:
        raise NotImplementedError(_MSG)

    def swipe_gesture(
        self, x0: int, y0: int, x1: int, y1: int,
        *, duration_ms: int = 400, timeout: float = 10.0,
    ) -> None:
        raise NotImplementedError(_MSG)

    def key(self, key: Key) -> None:
        raise NotImplementedError(_MSG)

    def input_text(self, text: str) -> None:
        raise NotImplementedError(_MSG)

    def launch(self, app_id: str, *, timeout: float = 10.0) -> None:
        raise NotImplementedError(_MSG)

    def cold_launch(
        self, app_id: str, *, settle_seconds: float = 1.0, timeout: float = 10.0
    ) -> None:
        raise NotImplementedError(_MSG)

    def force_stop(self, app_id: str, *, timeout: float = 10.0) -> None:
        raise NotImplementedError(_MSG)

    def kill_all_apps(self, *, timeout: float = 25.0) -> list[str]:
        raise NotImplementedError(_MSG)

    def setup_input_channel(self) -> bool:
        raise NotImplementedError(_MSG)

    def teardown_input_channel(self) -> None:
        raise NotImplementedError(_MSG)

    def start_recording(
        self, out_dir: Path, *, basename: str = "recording"
    ) -> RecordingHandle:
        raise NotImplementedError(_MSG)
