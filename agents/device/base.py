"""Cross-platform device backend abstraction.

`DeviceBackend` is the single seam between the agent/runtime layers and the
phone. The Android implementation (`agents/device/android.py`) drives adb;
iOS (WebDriverAgent) and HarmonyOS NEXT (hdc) are interface skeletons — see
docs/device_backends.md for the per-platform capability mapping.

Layering rule: code above this package never talks to a device tool
directly (adb / WDA / hdc) — it asks the backend. `agents/_adb.py` stays
around as a thin module-level shim over the default backend so the legacy
import surface keeps working while call sites migrate.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Protocol

from loguru import logger


@dataclass(frozen=True)
class UINode:
    """One node of the normalized accessibility tree, in document order.

    Per-platform sources:
      - Android: a `uiautomator dump` XML node — `text` / `content-desc` →
        ``desc`` / `resource-id` → ``resource_id`` / bounds ``[x1,y1][x2,y2]``.
      - iOS (planned): a WDA page-source element — `label` → ``desc``,
        accessibility identifier → ``resource_id``, `rect` → ``bounds``.
      - HarmonyOS (planned): a `uitest dumpLayout` node.

    ``bounds`` is ``(x1, y1, x2, y2)`` in screen pixels.
    """

    text: str = ""
    desc: str = ""
    resource_id: str = ""
    class_name: str = ""
    package: str = ""
    bounds: tuple[int, int, int, int] | None = None
    clickable: bool = False
    long_clickable: bool = False
    scrollable: bool = False
    focusable: bool = False
    enabled: bool = True

    @property
    def center(self) -> tuple[int, int] | None:
        """Center of ``bounds``; None when absent or zero/negative-area."""
        if self.bounds is None:
            return None
        x1, y1, x2, y2 = self.bounds
        if x2 <= x1 or y2 <= y1:
            return None
        return (x1 + x2) // 2, (y1 + y2) // 2


class Key(Enum):
    """Cross-platform logical keys. Android maps to KEYCODE_*; iOS BACK maps
    to an edge-swipe gesture (no system back key); HarmonyOS to uiInput
    keyEvent."""

    BACK = "back"
    HOME = "home"
    ENTER = "enter"


class RecordingHandle(Protocol):
    """Handle returned by `start_recording`; `.stop()` finalizes and returns
    the local video path (or a directory of chunks, or None on failure)."""

    def stop(self) -> Path | None: ...


class DeviceBackend(ABC):
    """Everything the runtime/agent layers need from a phone.

    Method semantics are platform-neutral; geometry-from-direction math
    (swipe distance, scroll reversal) stays in NativeEnv — backends only
    execute concrete gestures.
    """

    platform: ClassVar[str]

    # -- observation --------------------------------------------------------
    @abstractmethod
    def screencap(self, timeout: float = 5.0) -> Any | None:
        """Fresh screenshot as a PIL.Image, or None on failure (warned)."""

    @abstractmethod
    def screen_size(self, timeout: float = 5.0) -> tuple[int, int]:
        """(width, height) in pixels. Cached per backend instance."""

    @abstractmethod
    def dump_ui_tree(
        self, *, dump_timeout: float = 8.0, pull_timeout: float = 5.0
    ) -> list[UINode] | None:
        """Normalized accessibility tree as a flat list in document order,
        or None on any failure (logged at info — dumps can be flaky during
        animations). Consumers never see the platform's raw dump format."""

    @abstractmethod
    def foreground_app(self, *, timeout: float = 5.0) -> str | None:
        """App id (package / bundle id) of the foreground app, or None."""

    # -- gestures / input ----------------------------------------------------
    @abstractmethod
    def tap(self, x: int, y: int, *, timeout: float = 5.0) -> bool:
        """Tap pixel (x, y). False (with a warning) on failure."""

    @abstractmethod
    def long_press(self, x: int, y: int, *, duration_ms: int = 1000) -> None: ...

    def double_tap(self, x: int, y: int, *, timeout: float = 5.0) -> None:
        """Two quick taps at (x, y). Concrete default: two `tap` calls;
        backends override where a tighter gap is needed for the platform to
        register a double tap (Android combines them in one shell command —
        two subprocess round-trips are hundreds of ms apart)."""
        self.tap(x, y, timeout=timeout)
        self.tap(x, y, timeout=timeout)

    @abstractmethod
    def swipe_gesture(
        self, x0: int, y0: int, x1: int, y1: int,
        *, duration_ms: int = 400, timeout: float = 10.0,
    ) -> None:
        """One finger drag from (x0, y0) to (x1, y1)."""

    @abstractmethod
    def key(self, key: Key) -> None: ...

    @abstractmethod
    def input_text(self, text: str) -> None:
        """Type `text` into the focused field via the platform input channel
        (Android: AdbKeyboard broadcast; iOS: WDA /wda/keys)."""

    # -- app lifecycle -------------------------------------------------------
    @abstractmethod
    def launch(self, app_id: str, *, timeout: float = 10.0) -> None:
        """Launch the app (warm). Raises RuntimeError when it cannot start."""

    @abstractmethod
    def cold_launch(
        self, app_id: str, *, settle_seconds: float = 1.0, timeout: float = 10.0
    ) -> None:
        """force-stop + launch + settle (cold-launch policy, see CLAUDE.md)."""

    @abstractmethod
    def force_stop(self, app_id: str, *, timeout: float = 10.0) -> None: ...

    @abstractmethod
    def kill_all_apps(self, *, timeout: float = 25.0) -> list[str]:
        """Stop every running third-party app and return to the launcher;
        returns the app ids stopped. Between-task hard reset."""

    # -- input channel -------------------------------------------------------
    @abstractmethod
    def setup_input_channel(self) -> bool:
        """Prepare the text-input channel (Android: enable + set the
        AdbKeyboard IME). True when usable; failures warn loudly."""

    @abstractmethod
    def teardown_input_channel(self) -> None:
        """Restore the device's default input state (Android: `ime reset`)."""

    # -- recording -----------------------------------------------------------
    @abstractmethod
    def start_recording(
        self, out_dir: Path, *, basename: str = "recording"
    ) -> RecordingHandle: ...

    # -- platform hooks (concrete defaults) ----------------------------------
    def dismiss_permission_popup(self) -> str | None:
        """If a system permission/consent dialog is on top, accept it with the
        most-permissive Allow option. Returns the label tapped, or None when
        nothing was dismissed. Default: no such concept on this platform."""
        return None

    def swipe_down(
        self, ratio: float = 0.5, *, duration_ms: int = 300, timeout: float = 5.0
    ) -> None:
        """Finger-up swipe — pushes current content UP off screen, revealing
        content BELOW the viewport. Used by wait_for_reply capture_full to walk
        forward through a long agent reply.

        `ratio` is the vertical travel as a fraction of screen height (clamped
        to [0.1, 0.5]); `RELAY_CAPTURE_SCROLL_RATIO` overrides per call.
        Concrete here: pure screen_size + swipe_gesture composition.
        """
        env = os.getenv("RELAY_CAPTURE_SCROLL_RATIO")
        if env:
            try:
                ratio = float(env)
            except ValueError:
                logger.warning(f"Invalid RELAY_CAPTURE_SCROLL_RATIO={env!r}, using {ratio}")
        ratio = max(0.1, min(0.5, ratio))
        w, h = self.screen_size()
        x = w // 2
        travel = int(h * ratio)
        margin = int(h * 0.2)
        y_start = h - margin
        y_end = max(margin, y_start - travel)
        self.swipe_gesture(x, y_start, x, y_end, duration_ms=duration_ms, timeout=timeout)
