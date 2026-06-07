"""Shared ADB helpers — cold-launch, force-stop, base command builder.

Cold-launch policy (see CLAUDE.md / feedback_cold_launch_always.md): every
app open MUST be preceded by force-stop so the first observation is the
app's clean home surface — not a stale modal / chat thread / session sheet
from the previous run.

`RELAY_ANDROID_SERIAL` selects a specific device in multi-device setups
and is honored by every helper here.
"""
from __future__ import annotations

import os
import subprocess
import time

from loguru import logger

_SERIAL_ENV = "RELAY_ANDROID_SERIAL"


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


def screencap(timeout: float = 5.0):
    """Grab a fresh device screenshot as a PIL.Image (or None on failure).

    Used when RELAY_SKIP_STEP_SCREENSHOT is on and a step needs vision after
    all (tap_text / nm_ground_tap VLM fallback): the incoming observation
    may be a reused stale frame, so we capture our own current frame here.
    """
    import io

    from PIL import Image

    try:
        res = subprocess.run(
            adb_base() + ["exec-out", "screencap", "-p"],
            check=False, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("screencap timed out")
        return None
    if res.returncode != 0 or not res.stdout:
        logger.warning(
            f"screencap rc={res.returncode}: {(res.stderr or b'').decode(errors='replace').strip()}"
        )
        return None
    try:
        return Image.open(io.BytesIO(res.stdout)).convert("RGB")
    except Exception as e:  # pragma: no cover — decode guard
        logger.warning(f"screencap decode failed: {e}")
        return None


_screen_size: tuple[int, int] | None = None


def _get_screen_size(timeout: float = 5.0) -> tuple[int, int]:
    global _screen_size
    if _screen_size is not None:
        return _screen_size
    res = subprocess.run(
        adb_base() + ["shell", "wm", "size"],
        check=False, capture_output=True, text=True, timeout=timeout,
    )
    # Output like: "Physical size: 1080x2400" (and maybe "Override size: ...")
    w = h = 0
    for line in (res.stdout or "").splitlines():
        if "size:" in line and "x" in line:
            try:
                wh = line.split(":", 1)[1].strip().split("x")
                w, h = int(wh[0]), int(wh[1])
            except (ValueError, IndexError):
                continue
    if w == 0 or h == 0:
        # Fallback to a common phone resolution if parsing fails.
        w, h = 1080, 2400
        logger.warning(f"wm size parse failed, fallback to {w}x{h}")
    _screen_size = (w, h)
    return _screen_size


def swipe_down(
    ratio: float = 0.5,
    *,
    duration_ms: int = 300,
    timeout: float = 5.0,
) -> None:
    """Finger-up swipe — pushes current content UP off screen, revealing
    content BELOW the current viewport. Used by wait_for_reply capture_full
    to walk forward through a long agent reply (e.g. 小红书 点点) whose
    visible portion is the start; subsequent chunks live below.

    `ratio` is the vertical travel distance as a fraction of screen height
    (clamped to [0.1, 0.5]). Overridable per-call by env
    `RELAY_CAPTURE_SCROLL_RATIO`.
    """
    env = os.getenv("RELAY_CAPTURE_SCROLL_RATIO")
    if env:
        try:
            ratio = float(env)
        except ValueError:
            logger.warning(f"Invalid RELAY_CAPTURE_SCROLL_RATIO={env!r}, using {ratio}")
    ratio = max(0.1, min(0.5, ratio))
    w, h = _get_screen_size()
    x = w // 2
    travel = int(h * ratio)
    margin = int(h * 0.2)
    y_start = h - margin
    y_end = max(margin, y_start - travel)
    subprocess.run(
        adb_base() + [
            "shell", "input", "swipe",
            str(x), str(y_start), str(x), str(y_end), str(duration_ms),
        ],
        check=False, capture_output=True, text=True, timeout=timeout,
    )


def cold_launch(
    package: str,
    *,
    settle_seconds: float = 1.0,
    timeout: float = 10.0,
) -> None:
    """Force-stop + monkey LAUNCHER + settle. Raises on launch failure.

    `settle_seconds` defaults to 1.0. Our target apps have no splash *ad*, but
    most still show a brief brand splash (logo page) for ~0.5-1.0s after
    monkey LAUNCHER — measured on 千问: 0.5s catches the logo page, the chat
    home is fully rendered by 1.0s. 1.0s clears the brand splash without the
    wasted 2.5s. If an app you add DOES show a splash *ad* (skippable, longer),
    bump settle_seconds for that call site rather than re-globalizing the wait.
    """
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
