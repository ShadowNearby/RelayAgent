<h1 align="center">Device Setup</h1>

<p align="center">
  <b>What the device side needs before running RelayAgent (single-app debugging / NL flow / A/B benchmark)</b>
</p>

<p align="center">
  <b>English</b> | <a href="device_setup.zh.md">中文</a>
</p>

> One-shot health check: `uv run python scripts/validate/check_device_env.py [--benchmark relaybench|androiddaily|mobileworld|all]` (exit code 0 when nothing FAILs).
> For the emulator (no real device) path see [`emulator_testing.md`](emulator_testing.md).

## 💻 1. Host side

| Item | Requirement |
| --- | --- |
| adb | Android platform-tools, `adb` on PATH |
| Python | 3.12 (`uv venv --python 3.12 && uv sync --no-install-project`) |
| LLM endpoint | `.env` with `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` |
| MobileWorld (only for the A/B baseline / MW-fallback legs) | `third_party/MobileWorld` symlinked to a sibling checkout (machine-local, see [`mobileworld_real_device.md`](mobileworld_real_device.md)); otherwise the git snapshot pinned in pyproject is used |

## 📱 2. Device requirements (identical for real device and emulator)

| Item | Requirement | Notes |
| --- | --- | --- |
| Connection | USB debugging or Wi-Fi adb (`adb tcpip 5555` + `adb connect`) | The real-device A/B runs used Wi-Fi adb; **tasks that toggle flight mode kill the Wi-Fi adb transport and are excluded from the task set** |
| Multi-device | `RELAY_ANDROID_SERIAL=<serial>` | honored by every adb call (`agents/runtime/_adb.py`) |
| IME | **ADBKeyBoard** (`com.android.adbkeyboard`, [senzhk/ADBKeyBoard](https://github.com/senzhk/ADBKeyBoard)) installed | the runner does `ime enable/set` itself at startup and `ime reset` on exit; only "installed" is required |
| a11y dump | `uiautomator dump` works | primary path for tap_text grounding and reply scraping; without it everything falls back to the VLM (slow, expensive) |
| Screenshot | `adb exec-out screencap -p` works | measured ~1.5 s/frame on real devices — the dominant per-step cost |
| Stay awake | `settings put global stay_on_while_plugged_in 7` | prevents mid-task lock screens |
| Locale / network | Chinese vertical apps want a zh system locale + mainland network; Gemini / Copilot / Reddit / Booking need international network + Google services (GMS) | a dual-stack network setup is easiest |

## 📦 3. App requirements (by use case)

### 3.1 Core: the 10 manifest apps (all covered-tier gains live here)

Every benchmark's covered tier routes into the embedded agents of these 10 apps — **install and sign in**:

| App | Package | Account / prerequisites |
| --- | --- | --- |
| Tongyi Qwen | com.aliyun.tongyi | Alibaba account (shopping/food capabilities go through the Taobao backend; a fresh account can trip risk control — normal purchase history helps) |
| Amap | com.autonavi.minimap | signed in + location permission (ride hailing needs real-name / payment binding) |
| Ctrip | ctrip.android.view | signed in |
| WeChat | com.tencent.mm | signed in (Yuanbao / AI-search entry) |
| Xiaohongshu | com.xingin.xhs | signed in (Diandian AI search) |
| WPS Office | cn.wps.moffice_eng | signed in (AI doc/PPT) |
| Gemini | com.google.android.apps.bard | Google account + GMS + international network |
| Microsoft Copilot | com.microsoft.copilot | Microsoft account + international network |
| Reddit | com.reddit.frontpage | account + international network |
| Booking.com | com.booking | account + international network |

> No need to pre-clear permission dialogs by hand: `relay_agent`'s `_maybe_dismiss_permission_popup` auto-taps "Allow" for whitelisted packages (max 8 per task; `RELAY_DISMISS_PERMISSIONS=0` disables).

### 3.2 RelayBench (30 tasks)

Only the 10 apps from §3.1 (the suite is designed around them, balanced at 4–5 appearances per app).

### 3.3 AndroidDaily (235 tasks; the real-device A/B runs only the 71 covered)

- **Covered subset (71 tasks)**: instructions name Taobao/Eleme etc., but RA routes them into manifest apps (e.g. Taobao shopping → Qwen, same fulfillment backend) → still only §3.1.
- **MW-fallback tier (143 tasks, if run end-to-end on device)**: needs the native apps the tasks name. Top by frequency: Taobao (15), Ctrip (14), WeChat (14), Meituan (12), Amap (11), Railway 12306 (10), Xiaohongshu (9), Weibo (9), Qunar (9), Fliggy (8), JD (8), Pinduoduo (7), Bilibili, Douyin, Didi, Eleme, Dianping, NetEase Music, QQ Music, Zhihu… (70+ apps total — see the「APP名称」column of `benchmark/androiddaily_task_info.csv`). All need signed-in state.

### 3.4 MobileWorld (201 → 161 after `--skip-mcp`)

MW tasks run inside **MobileWorld's own app environment** (Mail, Messages, Mastodon, Files, Calendar, Mattermost, Chrome, Contacts, Gallery, Maps, Docreader, Clock, Settings, Camera, Taodian), with data seeded by MW's task initializers — **not the Chinese apps**. Environment setup: [`mobileworld_real_device.md`](mobileworld_real_device.md). Notes:

- `MCP-*` tasks (40) are tool-calls, not real GUI; excluded via `--skip-mcp`.
- Taodian (MW's bundled demo e-commerce app) fails on real devices for both systems (risk control); recorded as both-fail.

## 🧹 4. State hygiene before benchmark runs

- **Cold-launch before every app open** (`am force-stop` + monkey LAUNCHER; the runner does this automatically) — do not pre-open apps by hand and leave warm state.
- `kill_all_apps()` hard reset between tasks (force-stop all running third-party packages + HOME) so a previous task's chat thread / half-finished order can't leak into the next.
- Fairness switches (forced by `run_benchmark_test.py` by default): `RELAY_ROUTE_OVERLAY=0`, `RELAY_STEP_LOG=0`, `RELAY_CAPTURE_FULL_REPLY=0`, relay runs `--no-cache`. See [`evaluation.md`](evaluation.md) §8.

## ⚡ 5. Quick reference

```bash
# Health check (core checks + are the 10 manifest apps installed)
uv run python scripts/validate/check_device_env.py

# Per benchmark / custom app set / specific device
uv run python scripts/validate/check_device_env.py --benchmark mobileworld
uv run python scripts/validate/check_device_env.py --apps com.aliyun.tongyi,com.autonavi.minimap
uv run python scripts/validate/check_device_env.py --serial 46180DLAQ004LW
```
