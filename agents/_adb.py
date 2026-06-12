"""Legacy module-level shims over the default DeviceBackend.

The adb implementations moved to `agents/device/android.py` (AndroidBackend);
these same-named functions delegate to `agents.device.get_backend()` so the
existing import surface (`from agents._adb import screencap, ...`) keeps
working unchanged while call sites migrate. New code should hold a backend
instance instead:

    from agents.device import get_backend
    backend = get_backend()

`RELAY_ANDROID_SERIAL` is honored by the factory (read once at first use —
set it before the first device call, as every entry-point script does).
"""
from __future__ import annotations

from typing import Any

from agents.device import get_backend


def adb_base() -> list[str]:
    return get_backend().adb_base()  # type: ignore[attr-defined]  # Android-only


def tap(x: int, y: int, *, timeout: float = 5.0) -> bool:
    return get_backend().tap(x, y, timeout=timeout)


def keyevent(code: str, *, timeout: float = 10.0) -> None:
    get_backend().keyevent(code, timeout=timeout)  # type: ignore[attr-defined]  # Android-only


def foreground_package(*, timeout: float = 5.0) -> str | None:
    return get_backend().foreground_app(timeout=timeout)


def reset_airplane_mode(*, timeout: float = 10.0) -> bool:
    return get_backend().reset_airplane_mode(timeout=timeout)  # type: ignore[attr-defined]  # Android-only


def force_stop(package: str, *, timeout: float = 10.0) -> None:
    get_backend().force_stop(package, timeout=timeout)


def kill_all_apps(*, timeout: float = 25.0) -> list[str]:
    return get_backend().kill_all_apps(timeout=timeout)


def screencap(timeout: float = 5.0) -> Any | None:
    return get_backend().screencap(timeout=timeout)


def _get_screen_size(timeout: float = 5.0) -> tuple[int, int]:
    return get_backend().screen_size(timeout=timeout)


def swipe_down(ratio: float = 0.5, *, duration_ms: int = 300, timeout: float = 5.0) -> None:
    get_backend().swipe_down(ratio, duration_ms=duration_ms, timeout=timeout)


def cold_launch(package: str, *, settle_seconds: float = 1.0, timeout: float = 10.0) -> None:
    get_backend().cold_launch(package, settle_seconds=settle_seconds, timeout=timeout)


def dump_ui_tree(*, dump_timeout: float = 8.0, pull_timeout: float = 5.0):
    return get_backend().dump_ui_tree(dump_timeout=dump_timeout, pull_timeout=pull_timeout)


__all__ = [
    "adb_base", "tap", "keyevent", "foreground_package", "reset_airplane_mode",
    "force_stop", "kill_all_apps", "screencap", "swipe_down", "cold_launch",
    "dump_ui_tree",
]
