"""HarmonyBackend — DeviceBackend over `hdc` + the on-device `uitest` tool.

HarmonyOS NEXT / HarmonyOS 6 has no adb; `hdc` is the host-side device
connector and `uitest` is the on-device UI driver (the uiautomator + `input`
counterpart). The capability mapping mirrors AndroidBackend one-to-one — only
the wire commands differ:

  screencap            uitest screenCap -p <remote> + hdc file recv
  screen_size          derived from a screencap frame (cached per instance)
  dump_ui_tree         uitest dumpLayout -p <remote> (JSON) → hdc file recv → UINode
  foreground_app       aa dump -l  (the FOREGROUND mission's bundle)
  tap / long_press     uitest uiInput click / longClick <x> <y>
  swipe_gesture        uitest uiInput swipe <x0> <y0> <x1> <y1> <velocity>
  key BACK/HOME/ENTER  uitest uiInput keyEvent Back / Home / 2054
  input_text           uitest uiInput inputText <x> <y> <text>  (no IME swap)
  launch / force_stop  aa start -b <bundle> -a <ability> / aa force-stop <bundle>
  kill_all_apps        aa dump -l → aa force-stop each (minus launcher/systemui)
  setup_input_channel  no-op (uiInput inputText injects directly, no AdbKeyboard)
  start_recording      not yet supported on HarmonyOS (no-op handle, warns)
  permission popups    dumpLayout + label-driven Allow, same policy as Android

The serial is an instance attribute (the factory reads `RELAY_ANDROID_SERIAL`
for parity with Android, even though hdc selects targets with `-t`), so several
backends can coexist in one process.

VERIFICATION STATUS: the structure and hdc/uitest invocations follow the
documented CLIs, but several wire details (dumpLayout attribute names, the
ENTER keycode, `aa dump -l` foreground parsing, CJK over uiInput inputText)
have not yet been confirmed against a live HarmonyOS 6 device — every such
spot is flagged `NOTE(verify-on-device)`. Until then, treat this as
"implemented, unverified". Device-less unit tests pin the argv each method
composes and the pure JSON→UINode parser.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from loguru import logger

from agents.device.base import DeviceBackend, Key, RecordingHandle, UINode

# uitest uiInput keyEvent: "Back"/"Home"/"Power" are named; everything else is
# a numeric OHOS keycode. KEYCODE_ENTER == 2054.
# NOTE(verify-on-device): confirm 2054 maps to Enter on the target build.
_HDC_KEYS = {
    Key.BACK: "Back",
    Key.HOME: "Home",
    Key.ENTER: "2054",
}

# Bounds string as emitted by uitest dumpLayout: "[x1,y1][x2,y2]" (same shape
# as Android's uiautomator). Some builds emit an object instead — handled too.
_BOUNDS_BRACKET_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# uiInput swipe takes a velocity (px/s), not a duration; clamp to the range
# uitest accepts.
_SWIPE_VELOCITY_MIN = 200
_SWIPE_VELOCITY_MAX = 40000

# System bundles kill_all_apps must never force-stop (launcher/systemui/etc.).
# NOTE(verify-on-device): bundle ids vary by vendor build.
_PROTECTED_BUNDLES = {
    "com.huawei.hmos.launcher",
    "com.huawei.ohos.launcher",
    "com.ohos.launcher",
    "com.huawei.hmos.systemui",
    "com.ohos.systemui",
    "com.huawei.hmos.settings",
}

# Foreground bundle in `aa dump -l` mission blocks, e.g.
#   "... #1:com.example.app:EntryAbility ..."  -> com.example.app
_AA_BUNDLE_RE = re.compile(r"#\d+:([A-Za-z][\w.]+):")
_AA_BUNDLE_FALLBACK_RE = re.compile(r"bundle name \[([\w.]+)\]")


def _parse_bounds(raw: Any) -> tuple[int, int, int, int] | None:
    """Tolerate both uitest bounds encodings:
      - "[x1,y1][x2,y2]" string (most builds)
      - {"left","top","right","bottom"} object (some builds)
    Pure helper so it is unit-testable without a device."""
    if isinstance(raw, str):
        m = _BOUNDS_BRACKET_RE.search(raw)
        if m:
            return tuple(int(v) for v in m.groups())  # type: ignore[return-value]
        return None
    if isinstance(raw, dict):
        try:
            return (int(raw["left"]), int(raw["top"]),
                    int(raw["right"]), int(raw["bottom"]))
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _layout_to_nodes(data: dict) -> list[UINode]:
    """Flatten a `uitest dumpLayout` JSON tree into normalized UINodes in
    document (pre-order) order. Pure function — unit-testable from a captured
    sample.

    NOTE(verify-on-device): attribute names below follow the documented
    inspector schema; confirm against a real dumpLayout and adjust the
    `.get(...)` keys if a build labels them differently.
    """
    out: list[UINode] = []

    def _flag(attrs: dict, key: str) -> bool:
        return str(attrs.get(key, "")).lower() == "true"

    def _walk(node: dict) -> None:
        if not isinstance(node, dict):
            return
        attrs = node.get("attributes", node)  # some builds inline attributes
        if isinstance(attrs, dict):
            out.append(UINode(
                text=str(attrs.get("text", "")).strip(),
                # content-description equivalent
                desc=str(attrs.get("description", "")
                         or attrs.get("accessibilityText", "")).strip(),
                # the inspector "id"/"key" is the resource-id counterpart
                resource_id=str(attrs.get("id", "") or attrs.get("key", "")),
                class_name=str(attrs.get("type", "")),
                package=str(attrs.get("bundleName", "")),
                bounds=_parse_bounds(attrs.get("bounds")),
                clickable=_flag(attrs, "clickable"),
                long_clickable=_flag(attrs, "longClickable"),
                scrollable=_flag(attrs, "scrollable"),
                # uitest exposes the live "focused" state; "focusable" when present
                focusable=_flag(attrs, "focusable") or _flag(attrs, "focused"),
                # default true: only an explicit "false" disables a node
                enabled=str(attrs.get("enabled", "true")).lower() != "false",
            ))
        for child in node.get("children", []) or []:
            _walk(child)

    _walk(data)
    return out


def _parse_foreground_bundle(text: str) -> str | None:
    """Find the FOREGROUND mission's bundle in `aa dump -l` output.
    NOTE(verify-on-device): mission-block format varies by build."""
    # Split into per-mission blocks; the foreground one carries a FOREGROUND state.
    blocks = re.split(r"(?=Mission ID )", text)
    for blk in blocks:
        if "FOREGROUND" not in blk.upper():
            continue
        m = _AA_BUNDLE_RE.search(blk) or _AA_BUNDLE_FALLBACK_RE.search(blk)
        if m:
            return m.group(1)
    return None


class _NoRecording:
    """Placeholder RecordingHandle for platforms without shell screen-record."""

    def stop(self) -> Path | None:
        return None


class HarmonyBackend(DeviceBackend):
    platform = "harmonyos"

    # Device-side scratch dir reachable by the shell user.
    _REMOTE_SCREEN = "/data/local/tmp/relay_screen.png"
    _REMOTE_LAYOUT = "/data/local/tmp/relay_layout.json"

    def __init__(self, serial: str | None = None) -> None:
        self.serial = serial
        self._screen_size_cache: tuple[int, int] | None = None
        # uiInput inputText needs the target field's coordinates; the interface
        # only passes text, so we reuse the most recent tap point (the tap that
        # selected the field). None until the first tap.
        self._last_tap: tuple[int, int] | None = None
        self._input_channel_ok: bool | None = None

    # -- command plumbing ----------------------------------------------------
    def hdc_base(self) -> list[str]:
        """hdc argv prefix with this instance's `-t <serial>` target select."""
        return ["hdc"] + (["-t", self.serial] if self.serial else [])

    def _run(
        self, args: list[str], *, timeout: float, text: bool = True
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.hdc_base() + args,
            check=False, capture_output=True, text=text, timeout=timeout,
        )

    def _recv(self, remote: str, local: str, *, timeout: float) -> bool:
        """`hdc file recv <remote> <local>` — pull a device file to the host."""
        try:
            res = self._run(["file", "recv", remote, local], timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.info(f"hdc file recv {remote} failed: {exc}")
            return False
        if res.returncode != 0:
            logger.info(
                f"hdc file recv {remote} rc={res.returncode}: "
                f"{(res.stderr or res.stdout or '').strip()}"
            )
            return False
        return True

    # -- observation ---------------------------------------------------------
    def screencap(self, timeout: float = 5.0) -> Any | None:
        """`uitest screenCap` on device + `hdc file recv` → PIL.Image (or None).

        Two round trips (capture-to-file, then pull) — there is no clean
        binary-stream equivalent of Android's `exec-out screencap`."""
        import tempfile

        from PIL import Image

        try:
            cap = self._run(
                ["shell", "uitest", "screenCap", "-p", self._REMOTE_SCREEN],
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"uitest screenCap failed: {exc}")
            return None
        if cap.returncode != 0:
            logger.warning(
                f"uitest screenCap rc={cap.returncode}: "
                f"{(cap.stderr or cap.stdout or '').strip()}"
            )
            return None
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            local_png = fh.name
        try:
            if not self._recv(self._REMOTE_SCREEN, local_png, timeout=timeout):
                return None
            return Image.open(local_png).convert("RGB")
        except Exception as e:  # pragma: no cover — decode guard
            logger.warning(f"screencap decode failed: {e}")
            return None
        finally:
            try:
                Path(local_png).unlink()
            except OSError:
                pass

    def screen_size(self, timeout: float = 5.0) -> tuple[int, int]:
        """(width, height) in pixels, derived once from a screencap frame and
        cached. Falls back to a common phone resolution if capture fails."""
        if self._screen_size_cache is not None:
            return self._screen_size_cache
        img = self.screencap(timeout=timeout)
        if img is not None:
            self._screen_size_cache = (img.width, img.height)
            return self._screen_size_cache
        w, h = 1080, 2400
        logger.warning(f"screen_size: screencap unavailable, fallback to {w}x{h}")
        self._screen_size_cache = (w, h)
        return self._screen_size_cache

    def dump_ui_tree(
        self, *, dump_timeout: float = 8.0, pull_timeout: float = 5.0
    ) -> list[UINode] | None:
        """`uitest dumpLayout` (JSON) → pull → parse → UINode list, or None on
        any failure (info-level — dumps can be flaky during animations)."""
        import os as _os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
            local_json = fh.name
        try:
            dump = self._run(
                ["shell", "uitest", "dumpLayout", "-p", self._REMOTE_LAYOUT],
                timeout=dump_timeout,
            )
            if dump.returncode != 0:
                logger.info(f"uitest dumpLayout failed: {(dump.stderr or '').strip()}")
                return None
            if not self._recv(self._REMOTE_LAYOUT, local_json, timeout=pull_timeout):
                return None
            if not _os.path.getsize(local_json):
                logger.info("uitest dumpLayout pulled an empty file")
                return None
            try:
                with open(local_json, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.info(f"dumpLayout JSON parse error: {e}")
                return None
            return _layout_to_nodes(data)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.info(f"uitest dumpLayout path unavailable: {e}")
            return None
        finally:
            try:
                _os.unlink(local_json)
            except OSError:
                pass

    def foreground_app(self, *, timeout: float = 5.0) -> str | None:
        """Foreground app's bundle id via `aa dump -l`, or None on failure."""
        try:
            r = self._run(["shell", "aa", "dump", "-l"], timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if r.returncode != 0:
            return None
        return _parse_foreground_bundle(r.stdout or "")

    # -- gestures / input ----------------------------------------------------
    def _ui_input(self, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
        return self._run(["shell", "uitest", "uiInput", *args], timeout=timeout)

    def tap(self, x: int, y: int, *, timeout: float = 5.0) -> bool:
        try:
            res = self._ui_input("click", str(int(x)), str(int(y)), timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"tap ({x},{y}) failed: {exc}")
            return False
        if res.returncode != 0:
            logger.warning(f"tap ({x},{y}) rc={res.returncode}: "
                           f"{(res.stderr or res.stdout).strip()}")
            return False
        self._last_tap = (int(x), int(y))
        return True

    def long_press(self, x: int, y: int, *, duration_ms: int = 1000) -> None:
        # uiInput longClick has a fixed long-press duration; duration_ms is
        # accepted for interface parity but not forwarded.
        self._ui_input("longClick", str(int(x)), str(int(y)))
        self._last_tap = (int(x), int(y))

    def swipe_gesture(
        self, x0: int, y0: int, x1: int, y1: int,
        *, duration_ms: int = 400, timeout: float = 10.0,
    ) -> None:
        # uiInput swipe takes a velocity (px/s), not a duration — convert.
        dist = math.hypot(x1 - x0, y1 - y0)
        dt = max(duration_ms, 1) / 1000.0
        velocity = int(max(_SWIPE_VELOCITY_MIN,
                           min(_SWIPE_VELOCITY_MAX, dist / dt)))
        self._ui_input(
            "swipe", str(int(x0)), str(int(y0)), str(int(x1)), str(int(y1)),
            str(velocity), timeout=timeout,
        )

    def key(self, key: Key) -> None:
        try:
            res = self._ui_input("keyEvent", _HDC_KEYS[key])
            if res.returncode != 0:
                logger.warning(f"keyEvent {key} rc={res.returncode}: "
                               f"{(res.stderr or res.stdout).strip()}")
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"keyEvent {key} failed: {exc}")

    def input_text(self, text: str, *, timeout: float = 30.0) -> None:
        """Type into the focused field via `uiInput inputText <x> <y> <text>`.

        uiInput needs the field's coordinates; we reuse the most recent tap
        point (the tap that selected the field). No IME swap is required — the
        text is injected directly, so CJK works without an AdbKeyboard-style
        helper.

        NOTE(verify-on-device): CJK and text containing spaces/quotes traverse
        the hdc→device shell as a single argv; confirm round-trip fidelity on a
        real device (Android needed a base64 channel for the same reason)."""
        if self._last_tap is None:
            logger.warning(
                "input_text on HarmonyOS needs a target point but no tap has "
                "been recorded yet; falling back to screen center"
            )
            w, h = self.screen_size()
            x, y = w // 2, h // 2
        else:
            x, y = self._last_tap
        self._ui_input("inputText", str(x), str(y), text, timeout=timeout)

    # -- app lifecycle -------------------------------------------------------
    @staticmethod
    def _split_app_id(app_id: str) -> tuple[str, str]:
        """app_id is the bundle, optionally "bundle/ability"; default ability
        is EntryAbility (the HarmonyOS convention for the launch entry)."""
        if "/" in app_id:
            bundle, ability = app_id.split("/", 1)
            return bundle, ability
        return app_id, "EntryAbility"

    def launch(self, app_id: str, *, timeout: float = 10.0) -> None:
        bundle, ability = self._split_app_id(app_id)
        res = self._run(
            ["shell", "aa", "start", "-b", bundle, "-a", ability], timeout=timeout
        )
        combined = ((res.stdout or "") + (res.stderr or "")).lower()
        if res.returncode != 0 or "error" in combined or "failed" in combined:
            raise RuntimeError(
                f"Failed to launch {bundle}/{ability} via `aa start`. "
                f"stdout={res.stdout.strip()!r} stderr={res.stderr.strip()!r}"
            )

    def cold_launch(
        self, app_id: str, *, settle_seconds: float = 1.0, timeout: float = 10.0
    ) -> None:
        """force-stop + aa start + settle (cold-launch policy, see CLAUDE.md)."""
        logger.info(f"cold-launching {app_id} (force-stop + aa start) ...")
        self.force_stop(app_id, timeout=timeout)
        self.launch(app_id, timeout=timeout)
        time.sleep(settle_seconds)

    def force_stop(self, app_id: str, *, timeout: float = 10.0) -> None:
        bundle, _ = self._split_app_id(app_id)
        res = self._run(["shell", "aa", "force-stop", bundle], timeout=timeout)
        if res.returncode != 0:
            logger.warning(
                f"force-stop {bundle} rc={res.returncode}: "
                f"{(res.stderr or res.stdout).strip()}"
            )

    def kill_all_apps(self, *, timeout: float = 25.0) -> list[str]:
        """Force-stop every running (non-protected) app, then return home.

        HarmonyOS has no `pm list -3`; we enumerate live missions via
        `aa dump -l` and skip launcher/systemui. Best-effort: per-app failures
        only warn."""
        targets: list[str] = []
        try:
            r = self._run(["shell", "aa", "dump", "-l"], timeout=timeout)
            seen: set[str] = set()
            for m in _AA_BUNDLE_RE.finditer(r.stdout or ""):
                bundle = m.group(1)
                if bundle in _PROTECTED_BUNDLES or bundle in seen:
                    continue
                seen.add(bundle)
                targets.append(bundle)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"kill_all_apps enumerate failed: {exc}")
        for bundle in targets:
            self.force_stop(bundle)
        self.key(Key.HOME)
        logger.info(f"kill_all_apps: force-stopped {len(targets)} app(s): {targets}")
        return targets

    # -- input channel -------------------------------------------------------
    def setup_input_channel(self) -> bool:
        """No-op on HarmonyOS: `uiInput inputText` injects text directly, so
        there is no AdbKeyboard-style IME to enable. Always usable."""
        self._input_channel_ok = True
        logger.info("HarmonyOS input channel: uiInput inputText (no IME swap needed)")
        return True

    def teardown_input_channel(self) -> None:
        self._input_channel_ok = None

    # -- recording -----------------------------------------------------------
    def start_recording(
        self, out_dir: Path, *, basename: str = "recording"
    ) -> RecordingHandle:
        # HarmonyOS shell has no `screenrecord` equivalent; `uitest uiRecord`
        # records user *actions*, not video. Return a no-op handle so the
        # recording-enabled run path does not crash, and warn loudly.
        logger.warning(
            "start_recording: screen video recording is not supported on the "
            "HarmonyOS backend yet — proceeding without a recording"
        )
        return _NoRecording()

    # -- platform hooks -------------------------------------------------------
    def dismiss_permission_popup(self) -> str | None:
        """If the permission-manager dialog is on top, tap the most-permissive
        Allow button. Returns the label tapped, or None. Reuses the Android
        allow-label table (locale-generic strings)."""
        from agents.device.vendor_profiles import allow_labels

        # NOTE(verify-on-device): HarmonyOS permission-manager bundle id.
        permission_bundles = {
            "com.ohos.permissionmanager",
            "com.huawei.hmos.permissionmanager",
        }
        pkg = self.foreground_app(timeout=3)
        if pkg is None or pkg not in permission_bundles:
            return None
        nodes = self.dump_ui_tree(dump_timeout=2, pull_timeout=1)
        if nodes is None:
            logger.info(
                f"permission popup probe: foreground={pkg!r} but dumpLayout "
                "failed; cannot auto-dismiss"
            )
            return None
        for label in allow_labels():
            for n in nodes:
                if n.text != label and n.desc != label:
                    continue
                if not n.clickable:
                    continue
                center = n.center
                if center is None:
                    continue
                if not self.tap(*center, timeout=3):
                    return None
                logger.info(
                    f"dismissed permission popup: tapped {label!r} at {center} on {pkg}"
                )
                return label
        logger.warning(
            f"permission popup probe: foreground={pkg!r} but no known Allow "
            f"button found in dump"
        )
        return None
