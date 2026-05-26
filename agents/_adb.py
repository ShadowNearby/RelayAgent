"""Shared ADB helpers — cold-launch, force-stop, base command builder.

Cold-launch policy (see CLAUDE.md / feedback_cold_launch_always.md): every
app open MUST be preceded by force-stop so MobileWorld's first observation
is the app's clean home surface — not a stale modal / chat thread / session
sheet from the previous run.

`APPCARDS_ANDROID_SERIAL` selects a specific device in multi-device setups
and is honored by every helper here.
"""
from __future__ import annotations

import os
import subprocess
import time

from loguru import logger

_SERIAL_ENV = "APPCARDS_ANDROID_SERIAL"


def adb_base() -> list[str]:
    serial = os.getenv(_SERIAL_ENV)
    return ["adb"] + (["-s", serial] if serial else [])


def force_stop(package: str, *, timeout: float = 10.0) -> None:
    res = subprocess.run(
        adb_base() + ["shell", "am", "force-stop", package],
        check=False, capture_output=True, text=True, timeout=timeout,
    )
    if res.returncode != 0:
        logger.warning(
            f"force-stop {package} rc={res.returncode}: "
            f"{(res.stderr or res.stdout).strip()}"
        )


def cold_launch(
    package: str,
    *,
    settle_seconds: float = 2.5,
    timeout: float = 10.0,
) -> None:
    """Force-stop + monkey LAUNCHER + settle. Raises on launch failure."""
    logger.info(f"cold-launching {package} (force-stop + monkey LAUNCHER) ...")
    force_stop(package, timeout=timeout)
    res = subprocess.run(
        adb_base() + [
            "shell", "monkey", "-p", package,
            "-c", "android.intent.category.LAUNCHER", "1",
        ],
        check=False, capture_output=True, text=True, timeout=timeout,
    )
    if res.returncode != 0 or "No activities found" in (res.stdout + res.stderr):
        raise RuntimeError(
            f"Failed to launch {package} via adb monkey. "
            f"stdout={res.stdout.strip()!r} stderr={res.stderr.strip()!r}"
        )
    time.sleep(settle_seconds)
