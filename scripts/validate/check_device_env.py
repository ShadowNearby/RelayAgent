"""Pre-flight check for the device-side test environment.

Verifies, without driving any task, that a connected Android device (real or
emulator) is ready for RelayAgent runs: adb reachable, AdbKeyboard installed,
uiautomator dump + screencap working, and the apps a given benchmark needs are
installed. Companion doc: docs/device_setup.md / docs/device_setup.zh.md.

Usage:
    uv run python scripts/validate/check_device_env.py                      # core checks + all manifest apps
    uv run python scripts/validate/check_device_env.py --benchmark relaybench
    uv run python scripts/validate/check_device_env.py --apps com.aliyun.tongyi,com.autonavi.minimap
    RELAY_ANDROID_SERIAL=<serial> uv run python scripts/validate/check_device_env.py

Exit code 0 = no FAIL (WARNs allowed), 1 = at least one FAIL.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agents.runtime._adb import adb_base  # noqa: E402

IME_PACKAGE = "com.android.adbkeyboard"
IME_ID = "com.android.adbkeyboard/.AdbIME"

_results: list[tuple[str, str, str]] = []  # (level, name, detail)


def _report(level: str, name: str, detail: str = "") -> None:
    _results.append((level, name, detail))
    mark = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}[level]
    print(f"[{mark} {level}] {name}" + (f" — {detail}" if detail else ""))


def _adb(args: list[str], *, timeout: float = 15.0, binary: bool = False):
    return subprocess.run(
        adb_base() + args,
        check=False, capture_output=True, text=not binary, timeout=timeout,
    )


def check_adb_device() -> bool:
    try:
        res = subprocess.run(["adb", "version"], check=False, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        _report("FAIL", "adb binary", "`adb` not on PATH — install Android platform-tools")
        return False
    _report("PASS", "adb binary", res.stdout.splitlines()[0] if res.stdout else "")

    serial = os.getenv("RELAY_ANDROID_SERIAL")
    res = _adb(["get-state"])
    state = (res.stdout or "").strip()
    if state != "device":
        hint = f"RELAY_ANDROID_SERIAL={serial}" if serial else "check `adb devices` / USB debugging / Wi-Fi adb"
        _report("FAIL", "device connected", f"adb get-state={state or res.stderr.strip()!r} ({hint})")
        return False
    res = _adb(["shell", "getprop", "ro.build.version.release"])
    android_ver = (res.stdout or "").strip()
    res = _adb(["shell", "getprop", "ro.product.model"])
    model = (res.stdout or "").strip()
    qemu = (_adb(["shell", "getprop", "ro.kernel.qemu"]).stdout or "").strip()
    boot_qemu = (_adb(["shell", "getprop", "ro.boot.qemu"]).stdout or "").strip()
    kind = "emulator" if "1" in (qemu, boot_qemu) else "real device"
    _report("PASS", "device connected", f"{model} (Android {android_ver}, {kind}" + (f", serial={serial}" if serial else "") + ")")
    return True


def check_ime() -> None:
    res = _adb(["shell", "pm", "list", "packages", IME_PACKAGE])
    if IME_PACKAGE not in (res.stdout or ""):
        _report("FAIL", "AdbKeyboard IME", f"{IME_PACKAGE} not installed — text input will fail; "
                "install ADBKeyBoard.apk (github.com/senzhk/ADBKeyBoard)")
        return
    # The runner enables/sets the IME itself at startup; installed is enough.
    _report("PASS", "AdbKeyboard IME", f"{IME_ID} installed (runner enables it per run)")


def check_uiautomator() -> None:
    res = _adb(["shell", "uiautomator", "dump", "/sdcard/_relay_envcheck.xml"], timeout=30)
    out = (res.stdout or "") + (res.stderr or "")
    if "UI hierchary dumped" in out or "dumped to" in out:
        _report("PASS", "uiautomator dump", "a11y-tree scrape available")
        _adb(["shell", "rm", "-f", "/sdcard/_relay_envcheck.xml"])
    else:
        _report("WARN", "uiautomator dump", f"dump did not confirm ({out.strip()!r}) — "
                "tap_text grounding and reply scrape will fall back to VLM")


def check_screencap() -> None:
    t0 = time.monotonic()
    res = _adb(["exec-out", "screencap", "-p"], timeout=20, binary=True)
    dt = time.monotonic() - t0
    if res.returncode != 0 or not res.stdout or not res.stdout.startswith(b"\x89PNG"):
        _report("FAIL", "screencap", "could not capture a PNG frame")
        return
    size_kb = len(res.stdout) // 1024
    # ~1.5s/frame is the measured single-step cost ceiling on real devices.
    level = "PASS" if dt < 3.0 else "WARN"
    _report(level, "screencap", f"{dt:.2f}s for one frame ({size_kb} KB)"
            + ("" if dt < 3.0 else " — unusually slow; expect inflated wall-clock"))


def check_screen_settings() -> None:
    res = _adb(["shell", "wm", "size"])
    _report("PASS", "screen size", (res.stdout or "").strip().replace("\n", "; "))
    stay_on = (_adb(["shell", "settings", "get", "global", "stay_on_while_plugged_in"]).stdout or "").strip()
    if stay_on in ("0", "null", ""):
        _report("WARN", "stay-awake", "screen may sleep mid-task — run "
                "`adb shell settings put global stay_on_while_plugged_in 7`")
    else:
        _report("PASS", "stay-awake", f"stay_on_while_plugged_in={stay_on}")


def manifest_packages() -> list[str]:
    pkgs = []
    for f in sorted((ROOT / "manifests").glob("*.yaml")):
        pkgs.append(f.stem)
    return pkgs


def check_apps(packages: list[str]) -> None:
    res = _adb(["shell", "pm", "list", "packages"], timeout=30)
    installed = {ln.split("package:", 1)[1].strip()
                 for ln in (res.stdout or "").splitlines() if ln.startswith("package:")}
    missing = [p for p in packages if p not in installed]
    present = [p for p in packages if p in installed]
    if present:
        _report("PASS", f"apps installed ({len(present)}/{len(packages)})", ", ".join(present))
    if missing:
        _report("WARN", f"apps missing ({len(missing)}/{len(packages)})",
                ", ".join(missing) + " — tasks routed to these apps will fail; "
                "install + sign in (see docs/device_setup.md)")


def check_llm_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        _report("WARN", ".env", "missing — copy .env.example and fill LLM_BASE_URL/LLM_API_KEY/LLM_MODEL")
        return
    keys = {ln.split("=", 1)[0].strip() for ln in env_file.read_text().splitlines()
            if "=" in ln and not ln.lstrip().startswith("#")}
    missing = [k for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL") if k not in keys]
    if missing:
        _report("WARN", ".env", f"missing keys: {', '.join(missing)}")
    else:
        _report("PASS", ".env", "LLM endpoint configured")


def check_mobileworld() -> None:
    mw = ROOT / "third_party" / "MobileWorld"
    if mw.exists():
        _report("PASS", "MobileWorld runtime", f"{mw} resolves (symlink or checkout)")
    else:
        _report("WARN", "MobileWorld runtime",
                "third_party/MobileWorld not present — the A/B baseline and MW-fallback legs "
                "fall back to the pinned-git venv snapshot (see docs/mobileworld_real_device.md)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--benchmark", choices=["relaybench", "androiddaily", "mobileworld", "all"],
                    default=None, help="also check the app set this benchmark needs")
    ap.add_argument("--apps", default=None, help="comma-separated package names to check instead")
    ap.add_argument("--serial", default=None, help="device serial (overrides RELAY_ANDROID_SERIAL)")
    args = ap.parse_args()
    if args.serial:
        os.environ["RELAY_ANDROID_SERIAL"] = args.serial

    print("== RelayAgent device environment check ==")
    if not check_adb_device():
        print("\nDevice unreachable — remaining checks skipped.")
        return 1
    check_ime()
    check_uiautomator()
    check_screencap()
    check_screen_settings()

    if args.apps:
        check_apps([p.strip() for p in args.apps.split(",") if p.strip()])
    else:
        # Manifest apps are the covered-tier requirement for every benchmark.
        check_apps(manifest_packages())
    if args.benchmark in ("mobileworld", "all"):
        check_mobileworld()
    check_llm_env()

    fails = [r for r in _results if r[0] == "FAIL"]
    warns = [r for r in _results if r[0] == "WARN"]
    print(f"\n{len(_results)} checks: {len(_results) - len(fails) - len(warns)} pass, "
          f"{len(warns)} warn, {len(fails)} fail")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
