"""Default-backend factory.

`get_backend()` returns the process-wide default DeviceBackend, created on
first use from `RELAY_PLATFORM` (default "android") and, for Android,
`RELAY_ANDROID_SERIAL`. One process drives one device (per-leg subprocesses
each get their own env/serial); in-process multi-device runs construct
`AndroidBackend(serial=...)` instances directly instead of using the default.

Env is read ONCE at creation — set RELAY_PLATFORM / RELAY_ANDROID_SERIAL
before the first device call (every entry-point script already does).
"""
from __future__ import annotations

import os

from agents.device.base import DeviceBackend

_PLATFORM_ENV = "RELAY_PLATFORM"
_SERIAL_ENV = "RELAY_ANDROID_SERIAL"

_default_backend: DeviceBackend | None = None


def current_platform() -> str:
    """Normalized target platform: "android" (default) | "ios" | "harmonyos"."""
    raw = (os.getenv(_PLATFORM_ENV) or "android").strip().lower()
    return "harmonyos" if raw == "harmony" else raw


def get_backend() -> DeviceBackend:
    global _default_backend
    if _default_backend is None:
        _default_backend = _create(current_platform())
    return _default_backend


def _create(platform: str) -> DeviceBackend:
    if platform == "android":
        from agents.device.android import AndroidBackend

        return AndroidBackend(serial=os.getenv(_SERIAL_ENV))
    if platform == "ios":
        from agents.device.ios import IOSBackend

        return IOSBackend()
    if platform == "harmonyos":
        from agents.device.harmony import HarmonyBackend

        return HarmonyBackend()
    raise ValueError(
        f"unknown {_PLATFORM_ENV}={platform!r} (expected android | ios | harmonyos)"
    )


def set_default_backend(backend: DeviceBackend | None) -> None:
    """Override (or with None, reset) the process default — for tests and
    embedders that construct their own backend instance."""
    global _default_backend
    _default_backend = backend
