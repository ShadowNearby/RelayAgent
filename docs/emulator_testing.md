# Emulator Testing (the no-real-device path)

> 中文: [`emulator_testing.zh.md`](emulator_testing.zh.md)

The runtime is **pure Python over adb** (`screencap` / `uiautomator dump` / `input` / `monkey`, see `agents/runtime/native_runtime.py`) and depends on nothing device-specific — an Android emulator (AVD) is natively compatible, and screenshots are usually much faster than the ~1.5 s/frame of real devices. This page covers what you can test without a real device and how to set it up.

## 1. What runs with no device at all (pure LLM, zero adb)

The planning side never touches a device; a filled-in `.env` is enough:

```bash
uv run python scripts/run_plan.py --dry-run "帮我找一台适合学生的平板电脑，预算2000以内"   # NL-flow plan preview
uv run python scripts/run_benchmark_test.py --benchmark relaybench --plan-only            # plan-only stratification
uv run python -m unittest discover -s tests -v                                           # planner/runner unit tests
```

## 2. What the emulator can and cannot test

| Tier | Feasibility on emulator |
| --- | --- |
| Runtime smoke (screenshots, uiautomator, AdbKeyboard input, gestures, cold-launch) | ✅ fully — a green `check_device_env.py` is the gate |
| International app cards (Gemini / Copilot / Reddit / Booking) | ✅ needs a **Play Store image** + signed-in accounts; Gemini also needs a device-side Google account |
| Chinese vertical apps (Qwen / Amap / Ctrip / WeChat / Xiaohongshu / WPS) | ❌ **unusable on x86_64**: ARM-only, crash at launch under translation (WeChat measured SIGSEGV, see §8.1). **Covered-tier evaluation requires a real device** (or an ARM64 host + arm64 image) |
| MobileWorld benchmark | ✅ upstream MobileWorld is an emulator environment in the first place (ships its own Mail/Mastodon/Files apps + seeded data); the real-device setup is our extension (see [`mobileworld_real_device.md`](mobileworld_real_device.md)) |

Installing ARM-only Chinese apps on an x86_64 image relies on ARM translation (built-in from API 30+), but native-heavy apps (WeChat etc.) crash under it — see §8.1 for measurements. arm64-v8a images on Apple Silicon / ARM hosts avoid this.

## 3. Creating an AVD

> **Reference setup used while developing this repo**: AVD `relay-test` (**android-36.0-Baklava (Android 16) / google_apis_playstore / x86_64, pixel_9 profile 1080x2424**), KVM-accelerated, ~15 s cold boot, serial `emulator-5554`. The **playstore** image is chosen so international apps install officially from the Play Store (see §8). `sdkmanager`/`avdmanager`/`emulator` need `JAVA_HOME` (the snap Android Studio JBR works: `export JAVA_HOME=/snap/android-studio/current/jbr`).

```bash
# 1) after installing the SDK command-line tools (JAVA_HOME above):
sdkmanager "platform-tools" "emulator" "system-images;android-36.0-Baklava;google_apis_playstore;x86_64"

# 2) create the AVD (pixel_9 profile, 1080x2424)
echo no | avdmanager create avd -n relay-test \
  -k "system-images;android-36.0-Baklava;google_apis_playstore;x86_64" -d pixel_9 --force

# 3) boot (use -no-snapshot for cold state in evaluations; add -no-window on a headless server)
~/Android/Sdk/emulator/emulator -avd relay-test \
  -no-snapshot -no-boot-anim -no-audio -no-window -gpu swiftshader_indirect &
adb wait-for-device
# stop: adb -s emulator-5554 emu kill
```

> Image choice: the **playstore** image installs international apps officially but has a read-only system partition and no `adb root`; switch to a `google_apis` (non-playstore) image when you need `adb root` (sideloading, system edits). Both ship `libndk_translation.so` (ARM translation), but see §8 for the hard limit.

## 4. Device-side prep (same as a real device)

```bash
# AdbKeyboard (required for text input)
adb install ADBKeyBoard.apk        # github.com/senzhk/ADBKeyBoard

# keep the screen on
adb shell settings put global stay_on_while_plugged_in 7

# pin the emulator in multi-device setups
export RELAY_ANDROID_SERIAL=emulator-5554

# health check
uv run python scripts/validate/check_device_env.py
```

`check_device_env.py` detects and labels emulators via `ro.kernel.qemu` / `ro.boot.qemu`; all other checks are identical to a real device.

## 5. Suggested smoke path

1. `check_device_env.py` all green (IME / uiautomator / screencap).
2. Install + sign in to one international app (Copilot is the lightest), then run the single-app entry point:
   ```bash
   uv run python -m agents.runtime.native_runner com.microsoft.copilot "What is the tallest building in the world?"
   ```
   This exercises the full obs→predict→execute loop: cold-launch, entry taps, AdbKeyboard input, `wait_for_reply` text-hash done detection, reply scraping.
3. (Optional) real NL-flow run: `uv run python scripts/run_plan.py --yes "Ask Copilot ..."`.

## 6. Watching/controlling the emulator screen with scrcpy

With the emulator running headless (`-no-window`), mirror its screen via scrcpy (it streams from the device-side video encoder, independent of any native window; zero impact on the agent's `screencap`/`uiautomator` path, so it can stay open as a bystander). The adb server (5037) and the emulator (5554/5555) listen on `127.0.0.1` only — remote access requires an SSH tunnel.

**On the machine's own desktop** (from a non-desktop shell on a Wayland session, pass the session vars; from a desktop terminal a plain `scrcpy -s emulator-5554` suffices):

```bash
WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 SDL_VIDEODRIVER=wayland \
  scrcpy -s emulator-5554 --window-title "relay-test AVD"
```

**From a remote machine (the default — this repo's convention for watching the AVD)** — install scrcpy on the local machine with the display; the video stream rides the SSH tunnel to the server running the emulator (`user@emulator-host` below).

```bash
# Option B (preferred, scrcpy's official recipe, single adb server): tunnel the server-side adb server + video port
ssh -CN -L 15037:localhost:5037 -L 27183:localhost:27183 user@emulator-host  # terminal 1, keep open
export ADB_SERVER_SOCKET=tcp:127.0.0.1:15037           # terminal 2, local scrcpy ≥ 2.0
scrcpy -s emulator-5554 --force-adb-forward --tunnel-port=27183   # serial is the server-side emulator-5554

# Option A (fallback): tunnel the emulator's adbd port, connect the local adb to it
ssh -CN -L 15555:localhost:5555 user@emulator-host   # terminal 1
adb connect localhost:15555 && scrcpy -s localhost:15555                 # terminal 2
```

> **Prefer Option B.** Option A commonly fails with `Device is unauthorized` — the local adb's key isn't trusted by the emulator's adbd, and a headless emulator has no authorization popup to tap, so it hangs. Option B has the local scrcpy reuse the **server-side adb server that already handshook with the emulator**, bypassing local adb key auth entirely. Making A work would mean injecting the local public key into the emulator (`adb root` image only) or disabling `ro.adb.secure` — not worth it.

## 7. Running the on-device APK (android/ app) on the emulator

The debug build's `abiFilters` include `x86_64` (Chaquopy only reads abiFilters from `defaultConfig`, see `android/app/build.gradle.kts`), so the same APK installs on both real devices and the emulator:

```bash
cd android && JAVA_HOME=/snap/android-studio/current/jbr ANDROID_HOME=~/Android/Sdk ./gradlew :app:assembleDebug
adb install -r -t app/build/intermediates/apk/debug/app-debug.apk   # intermediates APK is flagged testOnly, hence -t

# enable the accessibility service without UI clicking (on real devices use system Settings)
adb shell settings put secure enabled_accessibility_services \
  com.relayagent.app/com.relayagent.app.RelayAccessibilityService
adb shell settings put secure accessibility_enabled 1
```

Fill the LLM gateway in the app's Settings page (same three values as `.env`); the MediaProjection consent dialog must be accepted once per run ("Start now", per-session since Android 14). Verified on the relay-test AVD: CPython boot, `OnDeviceAndroidBackend` injection, MediaProjection frames (feeding the grounding VLM), 3-stage routing + flow planning + in-process leg execution, traj/wall_clock under filesDir. **With no vertical apps installed the leg fails cleanly at cold-launch / grounding** — a full end-to-end pass still needs the target app installed and signed in (the §2 limitation).

## 8. Installing the manifest's vertical apps (measured findings)

The manifest has 10 apps in two source classes. **Bottom line: the x86_64 emulator is only good for installing international apps; ARM-only Chinese apps install but cannot run.**

### 8.1 The 6 Chinese apps (WeChat/Tongyi/Amap/Ctrip/WPS/Xiaohongshu) — unusable on x86_64 ❌

These are **ARM-only** (vendors ship no x86 build). Even with `libndk_translation.so` (ARM→x86 translation) present, **native-heavy apps crash at launch**:

- **WeChat, measured** (official Tencent CDN `dldir1v6.qq.com/weixin/android/...arm64.apk`, 250 MB): `adb install` succeeds and the icon appears, but launch crashes deterministically — `Fatal signal 11 (SIGSEGV)` in `wc_srvinit_1`, no process on retry.
- By inference the other 5 are high-risk too. **Covered-tier evaluation must use a real arm64 device** (consistent with §2); to run Chinese apps on an emulator, use an arm64-v8a image on an **Apple Silicon / ARM64 host** (no translation).
- Chinese vendor download pages are JS-driven with no direct links; aside from WeChat (official CDN direct link), the only sources are third-party mirrors — **untrusted, don't pull blindly.**

### 8.2 The 4 international apps (Gemini `com.google.android.apps.bard` / Copilot / Reddit / Booking) — via Play Store ✅

playstore image + a **Google sign-in the user performs**, then install from the Play Store (the store delivers the x86 split, runs natively, no translation).

- Sign-in is a manual step: `adb` cannot type credentials, so the user must use scrcpy (§6) to tap **Sign in**, enter the account/password, and accept the Play terms. **Claude does not enter credentials or accept agreements.**
- Land on the sign-in screen: `adb shell monkey -p com.android.vending -c android.intent.category.LAUNCHER 1` stops at `UnauthenticatedMainActivity`'s Sign in.
- After sign-in: `adb shell am start -a android.intent.action.VIEW -d 'market://details?id=<pkg>'` jumps to the app's detail page for a manual Install tap, or search in the store.

## 9. Known differences (emulator vs real device)

- Faster screenshots (typically <0.5 s/frame) → wall-clock numbers **must not be mixed into the same table as real-device numbers**; evaluation conclusions stay real-device.
- No cellular/SMS/NFC; location is simulated (Amap "nearby" tasks return unrealistic results).
- Some apps detect emulators and refuse or degrade (common with Chinese-app risk control).
- **ARM-only apps crash on x86_64** (§8.1) — a platform-level hard limit, not a config issue.
