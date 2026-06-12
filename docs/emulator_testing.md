# Emulator Testing (the no-real-device path)

> 中文: [`emulator_testing.zh.md`](emulator_testing.zh.md)

The runtime is **pure Python over adb** (`screencap` / `uiautomator dump` / `input` / `monkey`, see `agents/native_runtime.py`) and depends on nothing device-specific — an Android emulator (AVD) is natively compatible, and screenshots are usually much faster than the ~1.5 s/frame of real devices. This page covers what you can test without a real device and how to set it up.

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
| Chinese vertical apps (Qwen / Amap / Ctrip / WeChat / Xiaohongshu / WPS) | ⚠️ APKs sideload, but sign-in needs SMS verification and account risk control is much stricter on emulators (Qwen shopping / Amap ride hailing essentially unusable); **covered-tier evaluation still requires a real device** |
| MobileWorld benchmark | ✅ upstream MobileWorld is an emulator environment in the first place (ships its own Mail/Mastodon/Files apps + seeded data); the real-device setup is our extension (see [`mobileworld_real_device.md`](mobileworld_real_device.md)) |

Installing ARM-only Chinese apps on an x86_64 image relies on ARM translation (built-in from API 30+, slow and occasionally crashy); arm64-v8a images on Apple Silicon / ARM hosts avoid this.

## 3. Creating an AVD

```bash
# 1) after installing the SDK command-line tools:
sdkmanager "platform-tools" "emulator" "system-images;android-35;google_apis_playstore;x86_64"

# 2) create the AVD (the Pixel 8/9 profile matches our real-device 1080x2400)
avdmanager create avd -n relay-test -k "system-images;android-35;google_apis_playstore;x86_64" -d pixel_8

# 3) boot (use -no-snapshot for cold state in evaluations)
emulator -avd relay-test -no-snapshot -no-boot-anim &
adb wait-for-device
```

> For sideloading Chinese apps, a `google_apis` (non-playstore) image gives you `adb root`; stick to the playstore image when only testing international apps.

## 4. Device-side prep (same as a real device)

```bash
# AdbKeyboard (required for text input)
adb install ADBKeyBoard.apk        # github.com/senzhk/ADBKeyBoard

# keep the screen on
adb shell settings put global stay_on_while_plugged_in 7

# pin the emulator in multi-device setups
export RELAY_ANDROID_SERIAL=emulator-5554

# health check
uv run python scripts/check_device_env.py
```

`check_device_env.py` detects and labels emulators via `ro.kernel.qemu` / `ro.boot.qemu`; all other checks are identical to a real device.

## 5. Suggested smoke path

1. `check_device_env.py` all green (IME / uiautomator / screencap).
2. Install + sign in to one international app (Copilot is the lightest), then run the single-app entry point:
   ```bash
   uv run python -m agents.native_runner com.microsoft.copilot "What is the tallest building in the world?"
   ```
   This exercises the full obs→predict→execute loop: cold-launch, entry taps, AdbKeyboard input, `wait_for_reply` text-hash done detection, reply scraping.
3. (Optional) real NL-flow run: `uv run python scripts/run_plan.py --yes "Ask Copilot ..."`.

## 6. Known differences (emulator vs real device)

- Faster screenshots (typically <0.5 s/frame) → wall-clock numbers **must not be mixed into the same table as real-device numbers**; evaluation conclusions stay real-device.
- No cellular/SMS/NFC; location is simulated (Amap "nearby" tasks return unrealistic results).
- Some apps detect emulators and refuse or degrade (common with Chinese-app risk control).
