"""HarmonyBackend — DeviceBackend over HarmonyOS NEXT `hdc` + the `uitest` tool.

Mirrors `agents/device/android.py` method-for-method, swapping adb→hdc and the
uiautomator XML dump → `uitest dumpLayout` JSON. The serial is an instance
attribute (read from `RELAY_HARMONY_SERIAL` by the factory at creation); hdc
selects a target with `-t <connectKey>` (adb uses `-s`).

HarmonyOS NEXT has no adb. The counterparts:

  screencap            hdc shell uitest screenCap -p <path> + hdc file recv
  screen_size          hdc shell hidumper -s RenderService  (parsed, fallback)
  dump_ui_tree         hdc shell uitest dumpLayout -p <path> (JSON) → UINode list
  foreground_app       hdc shell aa dump -a         (foreground ability)
  tap / long_press     hdc shell uitest uiInput click / longClick
  swipe_gesture        hdc shell uitest uiInput swipe
  key BACK/HOME/ENTER  hdc shell uitest uiInput keyEvent Back / Home / 2054
  input_text           hdc shell uitest uiInput inputText
  launch / force_stop  hdc shell aa start -b <bundle> / aa force-stop <bundle>
  kill_all_apps        bm dump -a ∩ running (ps) + aa force-stop
  setup_input_channel  no-op (uiInput inputText needs no IME swap)
  start_recording      not wired yet (Phase 4) — returns None
  permission popups    dialogs appear in dumpLayout — reuse the vendor Allow tables

The exact hdc/uitest tokens that still need a real-device check are flagged
inline with `TODO(verify-on-device)`; the command/parse details are isolated in
small helpers (`hdc_base`, `_layout_to_nodes`, `_KEYNAMES`) so a single edit
fixes them once observed on hardware. Behavior contract (timeouts, best-effort
logging levels, caching) matches AndroidBackend so the runtime layers see no
difference between platforms.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from loguru import logger

from agents.device.base import DeviceBackend, Key, RecordingHandle, UINode

# Same "[x1,y1][x2,y2]" bounds dialect as Android's uiautomator dump — verified
# to match HarmonyOS `uitest dumpLayout` bounds strings on observed builds.
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# uitest `uiInput keyEvent` accepts named keys (Back/Home) or a numeric OHOS
# keycode; KEYCODE_ENTER = 2054. TODO(verify-on-device): confirm Enter token.
_KEYNAMES = {
    Key.BACK: "Back",
    Key.HOME: "Home",
    Key.ENTER: "2054",
}


def _attr(attrs: dict, *names: str) -> str:
    """First non-empty value among candidate attribute names (the dumpLayout
    schema varies across HarmonyOS releases — `id` vs `key`, `description` vs
    `content`)."""
    for name in names:
        v = attrs.get(name)
        if v:
            return str(v)
    return ""


def _flag(attrs: dict, name: str) -> bool:
    """Coerce a dumpLayout flag (bool or "true"/"false" string) to bool."""
    v = attrs.get(name)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() == "true"


def _layout_to_nodes(root: dict) -> list[UINode]:
    """Flatten a `uitest dumpLayout` JSON tree into normalized UINodes
    (document order). Pure function so it is unit-testable without a device.

    The dump is a nested ``{"attributes": {...}, "children": [...]}`` tree.
    Field mapping (TODO(verify-on-device) — isolated here for a one-line fix):
      text         -> text
      description  -> desc        (the content-desc analogue)
      id / key     -> resource_id
      type         -> class_name
      bundleName   -> package
      bounds       -> "[x1,y1][x2,y2]" -> (x1,y1,x2,y2)
      clickable / longClickable / scrollable / focusable / enabled -> flags
    """
    out: list[UINode] = []

    def _walk(node: dict) -> None:
        attrs = node.get("attributes")
        if isinstance(attrs, dict):
            bounds: tuple[int, int, int, int] | None = None
            m = _BOUNDS_RE.match(_attr(attrs, "bounds"))
            if m:
                bounds = tuple(int(v) for v in m.groups())  # type: ignore[assignment]
            out.append(UINode(
                text=_attr(attrs, "text").strip(),
                desc=_attr(attrs, "description", "content").strip(),
                resource_id=_attr(attrs, "id", "key", "accessibilityId"),
                class_name=_attr(attrs, "type"),
                package=_attr(attrs, "bundleName"),
                bounds=bounds,
                clickable=_flag(attrs, "clickable"),
                long_clickable=_flag(attrs, "longClickable"),
                scrollable=_flag(attrs, "scrollable"),
                focusable=_flag(attrs, "focusable"),
                enabled=_flag(attrs, "enabled") if "enabled" in attrs else True,
            ))
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    _walk(child)

    if isinstance(root, list):  # some builds wrap the forest in a top array
        for n in root:
            if isinstance(n, dict):
                _walk(n)
    elif isinstance(root, dict):
        _walk(root)
    return out


class HarmonyBackend(DeviceBackend):
    platform = "harmonyos"

    def __init__(self, serial: str | None = None) -> None:
        self.serial = serial
        self._screen_size_cache: tuple[int, int] | None = None
        # No IME swap is needed on HarmonyOS (uiInput inputText types directly),
        # so the input channel is considered usable from the start.
        self._input_channel_ok: bool | None = None
        # Device-side scratch paths (per-device storage; parameterized for tests).
        self._remote_dump_path = "/data/local/tmp/relay_layout.json"
        self._remote_cap_path = "/data/local/tmp/relay_screen.png"

    # -- command plumbing ----------------------------------------------------
    def hdc_base(self) -> list[str]:
        """hdc argv prefix with this instance's `-t <connectKey>` (HarmonyOS
        selects a target with -t, not adb's -s)."""
        return ["hdc"] + (["-t", self.serial] if self.serial else [])

    def _run(
        self, args: list[str], *, timeout: float, text: bool = True
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.hdc_base() + args,
            check=False, capture_output=True, text=text, timeout=timeout,
        )

    # -- observation ---------------------------------------------------------
    def screencap(self, timeout: float = 5.0) -> Any | None:
        """`uitest screenCap -p <dev>` + `hdc file recv` → PIL.Image (or None).

        Unlike adb's `exec-out screencap -p` (PNG on stdout), hdc has no
        stream-to-stdout screencap; uitest writes a PNG on-device and we pull it.
        """
        import os as _os
        import tempfile

        from PIL import Image

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            local = fh.name
        try:
            cap = self._run(
                ["shell", "uitest", "screenCap", "-p", self._remote_cap_path],
                timeout=timeout,
            )
            if cap.returncode != 0:
                logger.warning(
                    f"screenCap rc={cap.returncode}: {(cap.stderr or '').strip()}"
                )
                return None
            recv = self._run(
                ["file", "recv", self._remote_cap_path, local], timeout=timeout
            )
            if recv.returncode != 0 or not _os.path.getsize(local):
                logger.warning(
                    f"screencap file recv failed: {(recv.stderr or '').strip()}"
                )
                return None
            return Image.open(local).convert("RGB")  # convert() forces a load
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"screencap failed: {e}")
            return None
        except Exception as e:  # pragma: no cover — decode guard
            logger.warning(f"screencap decode failed: {e}")
            return None
        finally:
            try:
                _os.unlink(local)
            except OSError:
                pass

    def screen_size(self, timeout: float = 5.0) -> tuple[int, int]:
        if self._screen_size_cache is not None:
            return self._screen_size_cache
        w = h = 0
        try:
            # TODO(verify-on-device): confirm the hidumper service/section that
            # carries the physical resolution; parse the first "WxH" we find.
            res = self._run(
                ["shell", "hidumper", "-s", "RenderService", "-a", "screen"],
                timeout=timeout,
            )
            m = re.search(r"(\d{3,5})\s*[xX*]\s*(\d{3,5})", res.stdout or "")
            if m:
                w, h = int(m.group(1)), int(m.group(2))
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"screen_size probe failed: {exc}")
        if w == 0 or h == 0:
            w, h = 1080, 2400
            logger.warning(f"screen_size parse failed, fallback to {w}x{h}")
        self._screen_size_cache = (w, h)
        return self._screen_size_cache

    def dump_ui_tree(
        self, *, dump_timeout: float = 8.0, pull_timeout: float = 5.0
    ) -> list[UINode] | None:
        """`uitest dumpLayout -p <dev>` → pull → parse JSON → UINode list, or
        None on any failure (info-level — dumps can be flaky during animations).

        Timeouts are parameterized so the wait_for_reply precheck can use a tight
        budget, matching AndroidBackend.dump_ui_tree."""
        import os as _os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
            local = fh.name
        try:
            dump = self._run(
                ["shell", "uitest", "dumpLayout", "-p", self._remote_dump_path],
                timeout=dump_timeout,
            )
            if dump.returncode != 0:
                logger.info(f"uitest dumpLayout failed: {(dump.stderr or '').strip()}")
                return None
            pull = self._run(
                ["file", "recv", self._remote_dump_path, local], timeout=pull_timeout
            )
            if pull.returncode != 0 or not _os.path.getsize(local):
                logger.info(f"dumpLayout file recv failed: {(pull.stderr or '').strip()}")
                return None
            try:
                with open(local, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.info(f"dumpLayout JSON parse error: {e}")
                return None
            return _layout_to_nodes(data)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.info(f"uitest path unavailable: {e}")
            return None
        finally:
            try:
                _os.unlink(local)
            except OSError:
                pass

    def foreground_app(self, *, timeout: float = 5.0) -> str | None:
        """Foreground app's bundle id, or None on any failure.

        `aa dump -a` lists missions/abilities; the foreground one carries the
        bundle name. TODO(verify-on-device): pin the exact line format; the
        regex below scans for a `bundle name [<id>]` / `bundleName: <id>` token.
        """
        try:
            r = self._run(["shell", "aa", "dump", "-a"], timeout=max(timeout, 10.0))
            if r.returncode == 0:
                m = re.search(
                    r"bundle\s*name[\s:\[]+([\w.]+)", r.stdout or "", re.IGNORECASE
                )
                if m:
                    return m.group(1)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return None

    # -- gestures / input ----------------------------------------------------
    def tap(self, x: int, y: int, *, timeout: float = 5.0) -> bool:
        try:
            res = self._run(
                ["shell", "uitest", "uiInput", "click", str(int(x)), str(int(y))],
                timeout=timeout,
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
        # uitest has a dedicated longClick (no duration arg — fixed long press).
        # TODO(verify-on-device): if a duration is needed, fall back to a
        # same-point swipe like Android does.
        try:
            self._run(
                ["shell", "uitest", "uiInput", "longClick", str(int(x)), str(int(y))],
                timeout=10.0,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"long_press ({x},{y}) failed: {exc}")

    def swipe_gesture(
        self, x0: int, y0: int, x1: int, y1: int,
        *, duration_ms: int = 400, timeout: float = 10.0,
    ) -> None:
        # uitest `uiInput swipe fromX fromY toX toY [velocityPps]` takes a
        # velocity (px/s), not a duration. TODO(verify-on-device): map
        # duration_ms→velocity if precise timing matters; default velocity for now.
        self._run(
            ["shell", "uitest", "uiInput", "swipe",
             str(int(x0)), str(int(y0)), str(int(x1)), str(int(y1))],
            timeout=timeout,
        )

    def key(self, key: Key) -> None:
        try:
            res = self._run(
                ["shell", "uitest", "uiInput", "keyEvent", _KEYNAMES[key]],
                timeout=10.0,
            )
            if res.returncode != 0:
                logger.warning(f"keyEvent {key} rc={res.returncode}: "
                               f"{(res.stderr or res.stdout).strip()}")
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"keyEvent {key} failed: {exc}")

    def input_text(self, text: str, *, timeout: float = 30.0) -> None:
        """Type into the focused field via `uitest uiInput inputText`.

        No AdbKeyboard/IME swap is needed on HarmonyOS — uiInput types directly
        (CJK included). TODO(verify-on-device): some builds want
        `uiInput inputText <x> <y> <text>` (tap-to-focus first); if so, resolve
        the focused node's center from a dump before typing.
        """
        self._run(
            ["shell", "uitest", "uiInput", "inputText", text], timeout=timeout
        )

    # -- app lifecycle -------------------------------------------------------
    def launch(self, app_id: str, *, timeout: float = 10.0) -> None:
        """`aa start -b <bundle> [-a <ability>]`. Accepts "bundle/ability" to
        target a specific entry ability; bundle alone relies on the default
        entry. Raises RuntimeError on failure."""
        if "/" in app_id:
            bundle, ability = app_id.split("/", 1)
            args = ["shell", "aa", "start", "-b", bundle, "-a", ability]
        else:
            args = ["shell", "aa", "start", "-b", app_id]
        res = self._run(args, timeout=timeout)
        out = (res.stdout or "") + (res.stderr or "")
        if res.returncode != 0 or "error" in out.lower() or "failed" in out.lower():
            raise RuntimeError(
                f"Failed to launch {app_id} via aa start. "
                f"stdout={res.stdout.strip()!r} stderr={res.stderr.strip()!r}"
            )

    def cold_launch(
        self, app_id: str, *, settle_seconds: float = 1.0, timeout: float = 10.0
    ) -> None:
        """force-stop + launch + settle (cold-launch policy, see CLAUDE.md)."""
        logger.info(f"cold-launching {app_id} (force-stop + aa start) ...")
        self.force_stop(app_id, timeout=timeout)
        self.launch(app_id, timeout=timeout)
        time.sleep(settle_seconds)

    def force_stop(self, app_id: str, *, timeout: float = 10.0) -> None:
        # `aa force-stop` takes the bundle name (drop any "/ability" suffix).
        bundle = app_id.split("/", 1)[0]
        res = self._run(["shell", "aa", "force-stop", bundle], timeout=timeout)
        if res.returncode != 0:
            logger.warning(
                f"force-stop {bundle} rc={res.returncode}: "
                f"{(res.stderr or res.stdout).strip()}"
            )

    def kill_all_apps(self, *, timeout: float = 25.0) -> list[str]:
        """Force-stop every *running* installed bundle, then return home.

        Mirrors AndroidBackend: intersect installed bundles (`bm dump -a`) with
        live processes (`ps -A`, whose names are bundle ids on HarmonyOS) so we
        skip idle apps and never touch system services. Best-effort.
        """
        try:
            listed = self._run(["shell", "bm", "dump", "-a"], timeout=timeout)
            # bm dump -a prints bundle ids, one per (indented) line.
            installed = {
                ln.strip() for ln in (listed.stdout or "").splitlines()
                if "." in ln and " " not in ln.strip()
            }
            procs = self._run(["shell", "ps", "-A", "-o", "NAME"], timeout=timeout)
            running = {ln.strip().split(":", 1)[0]
                       for ln in (procs.stdout or "").splitlines()}
            targets = sorted(installed & running)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"kill_all_apps enumerate failed: {exc}")
            targets = []
        for bundle in targets:
            self.force_stop(bundle)
        self.key(Key.HOME)
        logger.info(f"kill_all_apps: force-stopped {len(targets)} running app(s): {targets}")
        return targets

    # -- input channel -------------------------------------------------------
    def setup_input_channel(self) -> bool:
        """No-op on HarmonyOS: `uitest uiInput inputText` types into the focused
        field without an IME swap, so there is no AdbKeyboard equivalent to
        install/enable. Always usable."""
        self._input_channel_ok = True
        return True

    def teardown_input_channel(self) -> None:
        self._input_channel_ok = None

    # -- recording -----------------------------------------------------------
    def start_recording(
        self, out_dir: Path, *, basename: str = "recording"
    ) -> RecordingHandle | None:
        # On-device recording (`uitest record`) is a later phase, mirroring the
        # iOS file-recording gap. Returning None keeps callers degrading
        # gracefully (no recording, no crash).
        logger.info("start_recording: not wired on HarmonyOS yet (Phase 4)")
        return None

    # -- platform hooks ------------------------------------------------------
    def dismiss_permission_popup(self) -> str | None:
        """If a system permission/consent dialog is on top, tap the
        most-permissive Allow button. Reuses the same vendor Allow-label tables
        as Android (HarmonyOS surfaces the dialog in dumpLayout too). Returns the
        label tapped, or None when nothing was dismissed."""
        from agents.device.vendor_profiles import allow_labels, permission_packages

        pkgs = permission_packages()
        labels = allow_labels()
        pkg = self.foreground_app(timeout=3)
        if pkg is None or pkg not in pkgs:
            return None
        nodes = self.dump_ui_tree(dump_timeout=2, pull_timeout=1)
        if nodes is None:
            logger.info(
                f"permission popup probe: foreground={pkg!r} but a11y "
                "dump failed; cannot auto-dismiss"
            )
            return None
        for label in labels:
            for n in nodes:
                if n.package not in pkgs:
                    continue
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
                    f"dismissed system permission popup: tapped {label!r} at "
                    f"{center} on {pkg}"
                )
                return label
        logger.warning(
            f"permission popup probe: foreground={pkg!r} but no known Allow "
            f"button found in dump (tried {len(labels)} labels)"
        )
        return None
