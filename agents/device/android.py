"""AndroidBackend — DeviceBackend over adb.

Function bodies migrated from `agents/_adb.py` (which is now a shim over the
default backend) and the IME/keyboard plumbing from `agents/native_runtime.py`.
The serial is an **instance attribute** (read from `RELAY_ANDROID_SERIAL` by
the factory at creation), so several backends can coexist in one process for
future device-pool runs; the per-leg subprocess boundary keeps using the env.

Cold-launch policy (see CLAUDE.md / feedback_cold_launch_always.md): every
app open MUST be preceded by force-stop so the first observation is the
app's clean home surface — not a stale modal / chat thread / session sheet
from the previous run.
"""
from __future__ import annotations

import base64
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from loguru import logger

from agents.device.base import DeviceBackend, Key, RecordingHandle, UINode

_ADB_KEYBOARD_PACKAGE = "com.android.adbkeyboard"
_ADB_KEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_FOCUS_PKG_RE = re.compile(r"\b([\w.]+)/[\w.$]+\b")

_KEYCODES = {
    Key.BACK: "KEYCODE_BACK",
    Key.HOME: "KEYCODE_HOME",
    Key.ENTER: "KEYCODE_ENTER",
}


def _xml_to_nodes(root: ET.Element) -> list[UINode]:
    """Flatten a uiautomator dump into normalized UINodes (document order).
    Pure function so it is unit-testable without a device."""
    out: list[UINode] = []
    for n in root.iter("node"):
        bounds: tuple[int, int, int, int] | None = None
        m = _BOUNDS_RE.match(n.get("bounds") or "")
        if m:
            bounds = tuple(int(v) for v in m.groups())  # type: ignore[assignment]

        def _flag(key: str) -> bool:
            return (n.get(key) or "").lower() == "true"

        out.append(UINode(
            text=(n.get("text") or "").strip(),
            desc=(n.get("content-desc") or "").strip(),
            resource_id=n.get("resource-id") or "",
            class_name=n.get("class") or "",
            package=n.get("package") or "",
            bounds=bounds,
            clickable=_flag("clickable"),
            long_clickable=_flag("long-clickable"),
            scrollable=_flag("scrollable"),
            focusable=_flag("focusable"),
            enabled=_flag("enabled"),
        ))
    return out


class AndroidBackend(DeviceBackend):
    platform = "android"

    def __init__(self, serial: str | None = None) -> None:
        self.serial = serial
        self._screen_size_cache: tuple[int, int] | None = None
        # None = setup never ran (assume the broadcast channel works, the
        # pre-backend behavior); True/False = setup_input_channel verdict.
        self._input_channel_ok: bool | None = None
        # Remote dump path is per-device storage, so two backends driving two
        # devices never collide; parameterized mostly for tests.
        self._remote_dump_path = "/sdcard/relay_window_dump.xml"

    # -- command plumbing ----------------------------------------------------
    def adb_base(self) -> list[str]:
        """adb argv prefix with this instance's `-s <serial>` (Android-only
        public surface, used by the `agents/_adb.py` shim and scripts that
        still compose raw adb commands)."""
        return ["adb"] + (["-s", self.serial] if self.serial else [])

    def _run(
        self, args: list[str], *, timeout: float, text: bool = True
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.adb_base() + args,
            check=False, capture_output=True, text=text, timeout=timeout,
        )

    # -- observation ---------------------------------------------------------
    def screencap(self, timeout: float = 5.0) -> Any | None:
        """`adb exec-out screencap -p` → PIL.Image (or None on failure)."""
        import io

        from PIL import Image

        try:
            res = self._run(["exec-out", "screencap", "-p"], timeout=timeout, text=False)
        except subprocess.TimeoutExpired:
            logger.warning("screencap timed out")
            return None
        if res.returncode != 0 or not res.stdout:
            logger.warning(
                f"screencap rc={res.returncode}: "
                f"{(res.stderr or b'').decode(errors='replace').strip()}"
            )
            return None
        try:
            return Image.open(io.BytesIO(res.stdout)).convert("RGB")
        except Exception as e:  # pragma: no cover — decode guard
            logger.warning(f"screencap decode failed: {e}")
            return None

    def screen_size(self, timeout: float = 5.0) -> tuple[int, int]:
        if self._screen_size_cache is not None:
            return self._screen_size_cache
        res = self._run(["shell", "wm", "size"], timeout=timeout)
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
        self._screen_size_cache = (w, h)
        return self._screen_size_cache

    def dump_ui_tree(
        self, *, dump_timeout: float = 8.0, pull_timeout: float = 5.0
    ) -> list[UINode] | None:
        """`uiautomator dump` → pull → parse → normalized UINode list, or None
        on any failure (info-level — dump can be flaky during animations).

        Timeouts are parameterized because the wait_for_reply precheck wants a
        tight budget (3s) — an 8s stall on every tick would burn the wall-clock
        budget when uiautomator is persistently unhealthy."""
        import os as _os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as fh:
            local_xml = fh.name
        try:
            dump = self._run(
                ["shell", "uiautomator", "dump", self._remote_dump_path],
                timeout=dump_timeout,
            )
            if dump.returncode != 0:
                logger.info(f"uiautomator dump failed: {(dump.stderr or '').strip()}")
                return None
            pull = self._run(
                ["pull", self._remote_dump_path, local_xml], timeout=pull_timeout
            )
            if pull.returncode != 0 or not _os.path.getsize(local_xml):
                logger.info(f"uiautomator dump pull failed: {(pull.stderr or '').strip()}")
                return None
            try:
                root = ET.parse(local_xml).getroot()
            except ET.ParseError as e:
                logger.info(f"window dump XML parse error: {e}")
                return None
            return _xml_to_nodes(root)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.info(f"uiautomator path unavailable: {e}")
            return None
        finally:
            try:
                _os.unlink(local_xml)
            except OSError:
                pass

    def foreground_app(self, *, timeout: float = 5.0) -> str | None:
        """Foreground app's package id, or None on any failure.

        Two probes, cheapest first:
          1. `dumpsys window` — mCurrentFocus / mFocusedApp lines (~100-300ms).
             NOTE: `dumpsys window windows` emits nothing on some builds
             (observed on this lab's pixel-class device); the bare subcommand
             is portable.
          2. `dumpsys activity activities` — (m)ResumedActivity line. Slower
             but catches builds where the window dump carries no focus line.
        """
        try:
            r = self._run(["shell", "dumpsys", "window"], timeout=timeout)
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    if "mCurrentFocus" not in line and "mFocusedApp" not in line:
                        continue
                    m = _FOCUS_PKG_RE.search(line)
                    if m:
                        return m.group(1)
        except (subprocess.TimeoutExpired, OSError):
            pass
        try:
            r = self._run(
                ["shell", "dumpsys", "activity", "activities"],
                timeout=max(timeout, 10.0),
            )
            if r.returncode == 0:
                m = re.search(r"ResumedActivity.*?\s([\w.]+)/", r.stdout)
                if m:
                    return m.group(1)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return None

    # -- gestures / input ----------------------------------------------------
    def tap(self, x: int, y: int, *, timeout: float = 5.0) -> bool:
        try:
            res = self._run(
                ["shell", "input", "tap", str(int(x)), str(int(y))], timeout=timeout
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"tap ({x},{y}) failed: {exc}")
            return False
        if res.returncode != 0:
            logger.warning(f"tap ({x},{y}) rc={res.returncode}: "
                           f"{(res.stderr or res.stdout).strip()}")
            return False
        return True

    def long_press(self, x: int, y: int, *, duration_ms: int = 1000) -> None:
        # Android has no `input longpress`; a same-point swipe of the wanted
        # duration is the canonical substitute.
        self.swipe_gesture(x, y, x, y, duration_ms=duration_ms)

    def swipe_gesture(
        self, x0: int, y0: int, x1: int, y1: int,
        *, duration_ms: int = 400, timeout: float = 10.0,
    ) -> None:
        self._run(
            ["shell", "input", "swipe",
             str(int(x0)), str(int(y0)), str(int(x1)), str(int(y1)), str(duration_ms)],
            timeout=timeout,
        )

    def key(self, key: Key) -> None:
        self.keyevent(_KEYCODES[key])

    def keyevent(self, code: str, *, timeout: float = 10.0) -> None:
        """Send a raw KEYCODE_* keyevent (Android-only public surface).
        Best-effort: failures only warn."""
        try:
            res = self._run(["shell", "input", "keyevent", code], timeout=timeout)
            if res.returncode != 0:
                logger.warning(f"keyevent {code} rc={res.returncode}: "
                               f"{(res.stderr or res.stdout).strip()}")
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"keyevent {code} failed: {exc}")

    def input_text(self, text: str, *, timeout: float = 30.0) -> None:
        # AdbKeyboard ADB_INPUT_B64 broadcast. Clean base64 (no surrounding
        # b'...' quotes — we pass an argv list so the keyboard receives the
        # decoded bytes directly).
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        self._run(
            ["shell", "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64],
            timeout=timeout,
        )

    # -- app lifecycle -------------------------------------------------------
    def launch(self, app_id: str, *, timeout: float = 10.0) -> None:
        res = self._run(
            ["shell", "monkey", "-p", app_id,
             "-c", "android.intent.category.LAUNCHER", "1"],
            timeout=timeout,
        )
        if res.returncode != 0 or "No activities found" in (res.stdout + res.stderr):
            raise RuntimeError(
                f"Failed to launch {app_id} via adb monkey. "
                f"stdout={res.stdout.strip()!r} stderr={res.stderr.strip()!r}"
            )

    def cold_launch(
        self, app_id: str, *, settle_seconds: float = 1.0, timeout: float = 10.0
    ) -> None:
        """Force-stop + monkey LAUNCHER + settle. Raises on launch failure.

        `settle_seconds` defaults to 1.0. Our target apps have no splash *ad*,
        but most still show a brief brand splash (logo page) for ~0.5-1.0s
        after monkey LAUNCHER — measured on 千问: 0.5s catches the logo page,
        the chat home is fully rendered by 1.0s. If an app you add DOES show a
        splash *ad* (skippable, longer), bump settle_seconds at that call site
        rather than re-globalizing the wait.
        """
        logger.info(f"cold-launching {app_id} (force-stop + monkey LAUNCHER) ...")
        self.force_stop(app_id, timeout=timeout)
        self.launch(app_id, timeout=timeout)
        time.sleep(settle_seconds)

    def force_stop(self, app_id: str, *, timeout: float = 10.0) -> None:
        res = self._run(["shell", "am", "force-stop", app_id], timeout=timeout)
        if res.returncode != 0:
            logger.warning(
                f"force-stop {app_id} rc={res.returncode}: "
                f"{(res.stderr or res.stdout).strip()}"
            )

    def kill_all_apps(self, *, timeout: float = 25.0) -> list[str]:
        """Force-stop every *running* third-party app, then return home.

        We force-stop only third-party packages (`pm list packages -3`) that
        actually have a live process (`ps -A`), so we skip the ~hundreds of
        installed-but-idle apps and never touch system packages. Best-effort:
        per-app failures only warn.
        """
        try:
            third = self._run(["shell", "pm", "list", "packages", "-3"], timeout=timeout)
            installed = {ln.split("package:", 1)[1].strip()
                         for ln in third.stdout.splitlines() if ln.startswith("package:")}
            procs = self._run(["shell", "ps", "-A", "-o", "NAME"], timeout=timeout)
            # process name may carry a `:service` suffix — the package is the prefix
            running = {ln.strip().split(":", 1)[0] for ln in procs.stdout.splitlines()}
            targets = sorted(installed & running)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"kill_all_apps enumerate failed: {exc}")
            targets = []
        for pkg in targets:
            self.force_stop(pkg)
        self.key(Key.HOME)
        logger.info(f"kill_all_apps: force-stopped {len(targets)} running app(s): {targets}")
        return targets

    # -- input channel -------------------------------------------------------
    def setup_input_channel(self) -> bool:
        """Enable + set AdbKeyboard as the active IME so input_text's
        `am broadcast -a ADB_INPUT_B64` is received. Returns True on success.

        We do NOT install it here — if it's missing, surface that loudly
        rather than silently degrading (per the surface-fallback-failures
        rule)."""
        installed = self._run(
            ["shell", "pm", "list", "packages", _ADB_KEYBOARD_PACKAGE], timeout=30.0
        )
        if _ADB_KEYBOARD_PACKAGE not in (installed.stdout or ""):
            logger.warning(
                f"AdbKeyboard ({_ADB_KEYBOARD_PACKAGE}) is NOT installed; input_text "
                "via ADB_INPUT_B64 will not work. Install ADBKeyboard.apk first."
            )
            self._input_channel_ok = False
            return False
        self._run(["shell", "ime", "enable", _ADB_KEYBOARD_IME], timeout=30.0)
        res = self._run(["shell", "ime", "set", _ADB_KEYBOARD_IME], timeout=30.0)
        active = self._run(
            ["shell", "settings", "get", "secure", "default_input_method"], timeout=30.0
        )
        ok = _ADB_KEYBOARD_PACKAGE in (active.stdout or "")
        if ok:
            logger.info(f"AdbKeyboard IME active: {(active.stdout or '').strip()}")
        else:
            logger.warning(
                f"ime set rc={res.returncode}; active IME still "
                f"{(active.stdout or '').strip()!r}"
            )
        self._input_channel_ok = ok
        return ok

    def teardown_input_channel(self) -> None:
        """Restore the device's default IME."""
        self._run(["shell", "ime", "reset"], timeout=30.0)
        self._input_channel_ok = None

    # -- recording -----------------------------------------------------------
    def start_recording(
        self, out_dir: Path, *, basename: str = "recording"
    ) -> RecordingHandle:
        # Lazy import: _recorder pulls in the _adb shim, which resolves back
        # through the factory to this class — import at call time, not module
        # import time, to keep that loop open.
        from agents import _recorder

        return _recorder.start(out_dir, basename=basename)

    # -- Android-only extras (not part of the DeviceBackend interface) -------
    def reset_airplane_mode(self, *, timeout: float = 10.0) -> bool:
        """Turn airplane mode OFF if a previous task left it ON. Returns True
        when it was on and we disabled it. Best-effort: failures only warn."""
        try:
            out = self._run(
                ["shell", "settings", "get", "global", "airplane_mode_on"],
                timeout=timeout,
            )
            if (out.stdout or "").strip() != "1":
                return False
            self._run(
                ["shell", "cmd", "connectivity", "airplane-mode", "disable"],
                timeout=timeout, text=False,
            )
            return True
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"reset_airplane_mode failed: {exc}")
            return False
