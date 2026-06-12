"""HarmonyBackend — interface skeleton for HarmonyOS NEXT via hdc.

NOT IMPLEMENTED yet: every method raises NotImplementedError. The class is
importable and instantiable so the factory can dispatch on
RELAY_PLATFORM=harmonyos and fail with a clear, single-line error at the
first device call. Full capability mapping: docs/device_backends.md.

Planned wiring (HarmonyOS NEXT has no adb; `hdc` + the `uitest` tool are the
counterparts — several mappings still need verification on a real device):

  screencap            hdc shell uitest screenCap <path> + hdc file recv
  screen_size          hdc shell hidumper -s RenderService …  (or uitest)
  dump_ui_tree         hdc shell uitest dumpLayout  (JSON) → UINode list
  foreground_app       hdc shell aa dump -a         (foreground ability)
  tap / long_press     hdc shell uitest uiInput click / longClick
  swipe_gesture        hdc shell uitest uiInput swipe
  key BACK/HOME        hdc shell uitest uiInput keyEvent Back / Home
  input_text           hdc shell uitest uiInput inputText
  launch / force_stop  hdc shell aa start -b <bundle> / aa force-stop
  kill_all_apps        enumerate via `bm dump -a` + aa force-stop
  setup_input_channel  no-op expected (uiInput inputText needs no IME swap;
                       CJK support to be verified)
  start_recording      hdc shell uitest record  (to be verified)
  permission popups    system dialogs appear in dumpLayout — same
                       label-driven Allow strategy as Android
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.device.base import DeviceBackend, Key, RecordingHandle, UINode

_MSG = (
    "HarmonyBackend is an interface skeleton — not implemented yet. "
    "See docs/device_backends.md for the hdc/uitest mapping."
)


class HarmonyBackend(DeviceBackend):
    platform = "harmonyos"

    def __init__(self, serial: str | None = None) -> None:
        # Constructor must not raise (see IOSBackend); only *use* fails.
        self.serial = serial

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
