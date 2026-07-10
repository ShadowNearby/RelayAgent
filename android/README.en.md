<h1 align="center">RelayAgent Android App</h1>

<p align="center">
  <b>Phase 1 skeleton: a pure accessibility approach + CPython via Chaquopy — no computer, no adb</b>
</p>

<p align="center">
  <b>English</b> | <a href="README.md">中文</a>
</p>

The RelayAgent runtime packaged as a standalone Android app: a **pure accessibility approach** (AccessibilityService gesture injection + uiautomator-format a11y tree serialization + MediaProjection screen capture), with **CPython embedded via Chaquopy** reusing the repo's `agents/` as-is (synced at build time by `syncRelayPython`). No computer, no adb.

## 🧱 Architecture mapping

| Host (Python over adb) | In-app |
| --- | --- |
| `adb exec-out screencap` (~1.5s/frame) | MediaProjection VirtualDisplay (~50ms/frame); a11y `takeScreenshot` as fallback |
| `uiautomator dump` XML | `A11yXmlSerializer` (uiautomator format; Python parses it unchanged) |
| `input tap/swipe` | `dispatchGesture` |
| AdbKeyboard broadcast input | a11y `ACTION_SET_TEXT` → clipboard `ACTION_PASTE` fallback |
| BACK/HOME/ENTER | `performGlobalAction` / `ACTION_IME_ENTER` |
| `monkey` cold launch | launch intent + `FLAG_ACTIVITY_CLEAR_TASK` (**no real force-stop; a known semantic drift**) |
| `.env` | Settings screen → EncryptedSharedPreferences → installed as env by the entrypoint |
| Terminal ask_user / EOF | Overlay panel: answer / take over (= None = handoff ends the run successfully) |
| Subprocess per leg | `InProcessLegExecutor` (`RELAY_LEG_EXECUTOR=inprocess`) |
| MobileWorld fallback | Replaced by the **general fallback**: `mw_fallback=False` + `allow_mw_legs=False` + `RELAY_MW_FALLBACK=0`; uncovered legs / the recovery ladder's last tier run a `type: general` leg — the manifest-free `GeneralGUIAgent` (step-by-step a11y-tree driving, in-process, capped by `RELAY_GENERAL_MAX_STEP`); only `RELAY_GENERAL_FALLBACK=0` (overridable in Settings) restores the old "cannot handle" behavior |
| `~/.relayagent/profile.yaml` (P3 memory layer) | `RELAY_PROFILE_ROOT=<filesDir>/profile`; the M3 pre-write y/n goes through the overlay ask_user; `RELAY_PROFILE`/`RELAY_TRAJ_REDACT`/`RELAY_RECOVERY` overridable from Settings via `_PASSTHROUGH_ENV` |
| scrcpy streaming capture + settle detection (P2) | Capture is already MediaProjection ~50ms/frame; settle detection is aligned — `OnDeviceAndroidBackend.wait_settled` polls `DeviceBridge.captureFrameSeq()` (the VirtualDisplay only receives frames on screen change, same property as scrcpy), "no new frame within the quiet window" = settled; `RELAY_SETTLE_DETECT=0` or a downed projection falls back to fixed sleeps |

## 🛠️ Building (needs a machine with Android Studio)

1. Open `android/` in Android Studio (first launch auto-installs SDK 34 / the Gradle wrapper).
2. Version check (**part of Spike A**): `app/build.gradle.kts` pins Chaquopy 16.0.0 + Python 3.12 — if that Chaquopy release doesn't support 3.12, drop `version` to `"3.11"` (the `agents/` code is 3.10+-compatible; the host repo's `requires-python` is unaffected).
3. `./gradlew :app:assembleDebug`, install on an arm64 device (minSdk 30 / Android 11+) or an x86_64 emulator (debug `abiFilters` include `x86_64`; drop it for a slimmer release APK). Headless builds need `JAVA_HOME=/snap/android-studio/current/jbr ANDROID_HOME=~/Android/Sdk` (or any JDK 17+ / SDK 34).

## 🧪 On-device instrumented tests (`app/src/androidTest/`)

Run on a connected device (real or AVD), **inside the app's own process** — no accessibility service, screen-capture consent, or LLM key needed:

- `PythonRuntimeTest` — on-device CPython boots; the full `entry.run_flow` import chain (agents.* + relay_android.*) imports; `JSONAction` behaves exactly as the host's `tests/test_action_model.py` pins; manifests + capability matrix installed to filesDir/relay `build_catalog`/`load_matrix` cleanly with matching app_ids; the filesDir Python reads via `jclass(DeviceBridge)` equals Kotlin's.
- `AssetInstallerTest` — assets unpack to filesDir/relay (manifests/*.yaml + matrix CSV); same-version reinstall is a no-op.
- `TrajLogTest` — a synthesized run tree parses into runs/legs/steps; bad JSON degrades gracefully as documented.
- `SettingsConfigTest` — `loadConfig` round-trips through EncryptedSharedPreferences (real Keystore); toggles default ON, blank fields stay blank. **Touched keys are backed up before and restored after**, so a configured gateway on the device is never wiped.
- `RunEventsTest` — `emit_status` JSON → typed main-thread events; unknown/bad payloads are dropped.
- `MainActivitySmokeTest` — conversational home (composer / suggestion chips / bubble rendering) + Examples/Log/Settings launch smoke. **Deliberately never taps Send**: that path needs the accessibility service and pops the MediaProjection consent.

How to run (avoid `connectedAndroidTest` — it uninstalls the APK afterwards, wiping app data):

```bash
./gradlew :app:assembleDebug :app:assembleDebugAndroidTest
adb install -r -t app/build/intermediates/apk/debug/app-debug.apk
adb install -r -t app/build/intermediates/apk/androidTest/debug/app-debug-androidTest.apk
adb shell am instrument -w com.relayagent.app.test/androidx.test.runner.AndroidJUnitRunner
```

Note: espresso-core must be ≥3.7 (older versions reflect on the removed `InputManager.getInstance` on Android 15+, failing all 5 UI tests).

## 📋 Spike checklist (verify in order; see the master plan §risks)

- **Spike A (Chaquopy feasibility)**: app starts → Python boots → `import agents.agent.action_model, yaml, PIL, loguru` succeeds → fill the gateway in Settings → run one direct LLM call through `relay_android.entry` (`RELAY_LLM_HTTP=1`, the stdlib HTTP shim).
- **Spike A2 (background-start exemption)**: with the app backgrounded, `DeviceBridge.launchApp("com.aliyun.tongyi")` brings the target to the foreground (a11y context + foreground service + SYSTEM_ALERT_WINDOW).
- **Spike B (dump fidelity)**: capture the same screen via `adb shell uiautomator dump` and the in-app `uiDumpXml()`; diff (text, content-desc, bounds) node sets with the host script; iterate `A11yXmlSerializer` until reply-relevant nodes align.

## 🎨 App UI

Material 3 theme (`res/values/themes.xml` + `colors.xml`, indigo brand color), viewBinding:

- **Home `MainActivity` (conversational)**: the whole screen is one task thread (`RecyclerView` + `ChatThread.kt`) — user tasks are right-side bubbles (`item_chat_user`), a running task is a live activity card (`item_chat_working`: spinner + one ▸/✓ row per subtask + current step line, fed by `RunEvents`), results are left-side cards (`item_chat_answer`: outcome + reply text + "view run details" → `RunDetailActivity`). A pill-shaped composer is pinned at the bottom (send morphs into stop while running); an empty thread shows a greeting + 3 example suggestions (from `res/raw/examples.json`). Banners at the top prompt for accessibility / gateway setup (tap-through to Settings); history / examples / settings live in the toolbar (`menu/main.xml`). The thread lives in the in-memory singleton `ChatStore` (`ChatThread.kt`); durable records stay in the trajectory log viewer.
- **`RunEvents.kt`**: `emit_status` JSON → typed event bus (`LegStart`/`Step`/`LegEnd`/`AskUser`/`AskAnswered`), dispatched by `OverlayController.postStatus` (floating chip / RunLog / chat thread all fed from one source); `DeviceBridge.askUser` posts ask events around the blocking call so the thread shows "waiting for your answer".
- **Examples `ExamplesActivity`**: reads `res/raw/examples.json` (50 entries: 30 RelayBench + 20 AndroidDaily, generated from `benchmark/` by `scripts/android/gen_app_examples.py`; regenerate after changing the benchmarks), cards + tags (source / app / category / difficulty), tap to prefill the composer.
- **Run logs (structured viewer)**: three levels — `LogActivity` (run list: original request / time / app tags / subtask count, newest first; **overflow menu: clear all logs**, with a privacy note that step screenshots may contain sensitive content) → `RunDetailActivity` (task card + one card per subtask: status badge / steps / wall-clock / tokens / reply preview) → `LegDetailActivity` (step timeline: annotated screenshot thumbnails + action type + coordinates/params + thought; tap a thumbnail for the full frame). Parsing lives in `TrajLog.kt` (reads `meta.json` / `summary.json` / `wall_clock.json` / `agent_reply.json` / `leg_verdict.json` / `steps/steps.json`, degrading gracefully), app names in `AppLabels.kt`; the raw file tree lives behind the toolbar overflow ("view raw files") → `RawLogActivity` (+ `LogDetailActivity`: pretty-printed JSON / PNGs in an ImageView). `entry.py` writes `meta.json` (original request + kind) so the viewer can show what each run was.
- **Live log**: the `RunLog` singleton ring buffer. `OverlayController.postStatus` formats `emit_status` events (`leg_start` / `leg_end` / `step`) into short lines, feeding both the floating chip and the home log card. The chip / ask_user panel use rounded drawables.

## 🔌 Wiring status

- **The `agents.device` injection seam is wired**: `relay_android/backend.py:install()` registers `OnDeviceAndroidBackend` via `set_default_backend`, verified on the AVD (CPython boot → backend injection → MediaProjection frames → three-stage routing → flow planning → in-process legs → traj under filesDir). Emulator setup and APK install steps: [`../docs/emulator_testing.zh.md`](../docs/emulator_testing.zh.md) §7.
- Host-side reused pieces: in-process `run_leg`, `InProcessLegExecutor`, `InteractionProvider`, the `make_llm_client` HTTP shim, `nl_flow.plan_request/execute_plan`, `RELAY_TRAJ_ROOT` redirection. `native_runner._agent_spec` falls back to the package module spec when `agents/agent/relay_agent.py` doesn't exist on disk (the Chaquopy AssetFinder packaging).

## 🔒 Security & privacy

- **Irreversible-action rails**: card capabilities use `handoff_to_user_required` + `stop_before` to **stop and hand control back** before pay/order CTAs; the general fallback agent (`GeneralGUIAgent`) carries a bilingual CTA stop-list (pay / transfer / order / booking...) and converts any hit into an ask_user handoff — it **never crosses one on its own**.
- **System permission-popup auto-accept**: runtime permission dialogs raised by the target app (camera / location / mic...) are auto-accepted with the most permissive Allow by default (never Deny; only fires when the foreground package is a known permission controller; capped at 8 per task). This means the agent grants the target app the runtime permissions it asks for — if you don't want that, **turn off "auto-allow permission popups" in Settings** (= `RELAY_DISMISS_PERMISSIONS=0`) and dialogs will wait for your tap.
- **Run logs contain raw screenshots**: every step under `traj_logs/` stores a screen frame (chats, balances, addresses may all be in there). Review before sharing debug logs; `RELAY_TRAJ_REDACT=1` only replaces profile text values — it does **not** scrub screenshot pixels.
- **User profile stays local**: `filesDir/profile/profile.yaml`, on-device only, gone with an app-data wipe; every write is preceded by a y/n confirmation.

## 📁 Runtime data layout (filesDir)

```
files/
  relay/manifests/*.yaml          # unpacked by AssetInstaller (synced from the repo at build time)
  relay/app_capability_matrix.csv
  relay/_generated/               # plan cache (same shape as the host's manifests/_generated)
  traj_logs/<ts>_plan_<apps>/NN_<id>/{traj.json,steps/,...}   # same shape as the host
```
