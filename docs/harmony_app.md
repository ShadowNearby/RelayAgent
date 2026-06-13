# HarmonyOS App Implementation

中文: [`harmony_app.zh.md`](harmony_app.zh.md) · TODO / on-device checklist: [`harmony_app_todo.md`](harmony_app_todo.md)

Packs RelayAgent into a standalone HarmonyOS NEXT app, the counterpart of the Android app
(`android/`). Two key decisions:

1. **The on-device runtime is rewritten in ArkTS** (it does NOT reuse the `agents/` Python).
   HarmonyOS NEXT is ArkTS/ArkUI with **no Chaquopy / CPython embedding**, so a native rewrite
   is idiomatic. Cost: the runtime now exists in two languages (Python host + ArkTS device);
   the "contract alignment" pins below guard against drift.
2. **Host-side `agents/device/harmony.py` is implemented over hdc/uitest**, mirroring the
   already-implemented `android.py`, replacing the old `NotImplementedError` skeleton.

Scope shipped: **host `harmony.py` + the minimal single-app loop** (`obs→predict→execute`).
Full VLM grounding and the NL cross-app flow port are Phase 2/3 — see the TODO checklist.

---

## Part A — host `agents/device/harmony.py` (hdc/uitest)

Mirrors [`agents/device/android.py`](../agents/device/android.py) method-for-method: adb→hdc,
uiautomator XML → `uitest dumpLayout` JSON. Device selection uses hdc's `-t <connectKey>` (adb
uses `-s`); the serial comes from `RELAY_HARMONY_SERIAL`.

| DeviceBackend method | hdc command |
| --- | --- |
| `screencap` | `uitest screenCap -p <path>` + `hdc file recv` → PIL |
| `screen_size` | `hidumper -s RenderService` (cached + 1080×2400 fallback) |
| `dump_ui_tree` | `uitest dumpLayout -p <path>` (JSON) → `_layout_to_nodes` → `UINode[]`, timeouts parameterized |
| `foreground_app` | `aa dump -a` (regex bundle) |
| `tap` / `long_press` / `swipe_gesture` | `uitest uiInput click / longClick / swipe` |
| `key` | `uitest uiInput keyEvent Back / Home / 2054` |
| `input_text` | `uitest uiInput inputText "<text>"` (no IME; CJK direct) |
| `launch` / `force_stop` | `aa start -b <bundle>[ -a <ability>]` / `aa force-stop <bundle>` |
| `cold_launch` | force_stop + launch + settle (same as android) |
| `kill_all_apps` | `bm dump -a` ∩ `ps -A` → force-stop loop + HOME |
| `setup/teardown_input_channel` | no-op returning True (uiInput needs no IME swap) |
| `start_recording` | not wired (Phase 4), returns None + info log (parity with the iOS gap) |
| `dismiss_permission_popup` | reuses the `vendor_profiles` Allow-label strategy (dialogs appear in dumpLayout) |

Implementation notes:
- `hdc_base()` / `_run(args, timeout, text)` is the same subprocess wrapper shape as `android._run`.
- `_layout_to_nodes(json)` is a pure function: recursively walks the dumpLayout `{attributes,
  children}` tree producing `UINode[]` in document order; the field mapping is isolated in one
  place (`text`/`description`/`id|key`/`type`/`bundleName`/`bounds`/flags), reusing the
  `[x1,y1][x2,y2]` bounds regex. Unit-testable without a device.
- `_KEYNAMES = {BACK:"Back", HOME:"Home", ENTER:"2054"}`.
- Logging levels match android: observe failure warning / dump parse info / fallback miss info|warning.

**Wiring**: [`factory.py`](../agents/device/factory.py) builds `HarmonyBackend(serial=RELAY_HARMONY_SERIAL)`
when `RELAY_PLATFORM=harmonyos` (`harmony` normalizes to `harmonyos`).

**Tests** (device-less): [`tests/test_harmony_backend.py`](../tests/test_harmony_backend.py) mocks
`agents.device.harmony.subprocess.run` and pins argv construction (`-t` injection), JSON→UINode
parsing, timeout/parse errors, `screen_size`/`input_channel` caching, `foreground_app`, `launch`
bundle/ability split, and factory dispatch. `test_device_backend.py` was updated (HarmonyBackend is
no longer a NotImplementedError stub).

---

## Part B — `harmony/` ArkTS app (minimal single-app loop)

A DevEco/hvigor project mirroring `android/`. The on-device runtime is entirely ArkTS — no
Python/Chaquopy/NAPI.

### File correspondence

| Android (Kotlin + Chaquopy) | HarmonyOS (ArkTS) |
| --- | --- |
| `RelayAccessibilityService.kt` | `ets/device/RelayAccessibilityExtension.ets` (`AccessibilityExtensionAbility`) |
| `DeviceBridge.kt` | `ets/device/DeviceBridge.ets` (same-language facade) |
| `ScreenCaptureService.kt` (MediaProjection) | `ets/device/ScreenCapture.ets` (AVScreenCapture, **Phase 2**) |
| `OverlayController.kt` (SYSTEM_ALERT_WINDOW) | `ets/device/OverlayController.ets` (`@ohos.window` floating) |
| `SettingsActivity.kt` (EncryptedSharedPreferences) | `ets/settings/SettingsStore.ets` (`@ohos.data.preferences`) |
| `MainActivity.kt` | `ets/entryability/EntryAbility.ets` + `ets/pages/Index.ets` |
| `AssetInstaller.kt` | `ets/device/AssetInstaller.ets` (reads rawfile directly) |
| `A11yXmlSerializer.kt` (→ XML) | builds `UINode[]` directly (`RelayAccessibilityExtension.walk`) |
| Chaquopy-embedded `agents/` | `ets/relay/*` (ArkTS rewrite) |

### ArkTS runtime (`ets/relay/`, vs `agents/`)

| ArkTS | Python counterpart | Notes |
| --- | --- | --- |
| `device.ets` | `device/base.py` + `relay_android/backend.py` | `DeviceBackend` interface + `UINode` (with `center`) + `Key` + `OnDeviceHarmonyBackend` (over DeviceBridge, async) |
| `actionModel.ets` | `action_model.py` | `JSONAction` + action-type constants; field order / validator (index↔xy exclusion) / `modelDump` / `equals` aligned |
| `nativeEnv.ets` | `native_runtime.py` | `NativeEnv` dispatch (scroll **reversal** + swipe geometry `unit=W/10*2` + `skip_screenshot`) + `runTask` loop + `Observation` |
| `interaction.ets` | `interaction.py` + `relay_android/interaction.py` | `InteractionProvider`; `askUser` null = take-over = success terminal |
| `llmClient.ets` | `llm_client.py` (HttpChatClient) | `@ohos.net.http` POST `/chat/completions`, no streaming, response normalized to a typed interface |
| `agent.ets` | subset of `relay_agent.py`+`action_planner.py`+`capability_router.py` | `predict`: first frame routeCapability (1 LLM) + buildPlan, then step-by-step materialize |
| `nativeRunner.ets` | `native_runner.py:run_leg` | single-app entry `runLeg(card, goal, cfg)` |

**The minimal loop supports deterministic + a11y-only steps only**: `open_app` / `tap_text`
(a11y tree match, no VLM) / `input_text` / `wait_for_reply` (text-hash stability, no VLM). Cards are
supplied as JSON produced by the host sync script.

### Data assets (build-time sync)

ArkTS has no YAML parser, so [`scripts/sync_harmony_assets.py`](../scripts/sync_harmony_assets.py)
converts `manifests/*.yaml` into slim JSON cards (`{app_id, app_name, agent_name,
capabilities:[{id, description, input_hint}]}`) and copies the capability matrix CSV into
`harmony/entry/src/main/resources/rawfile/relay/` (gitignored, generated before the build).

### Build (headless, works)

CLT lives at `/opt/command-line-tools-for-hmos` (HarmonyOS 6.1.1 / API 24 / hvigor 6.24.2, see
`/opt/harmonyos-env.sh`):

```bash
uv run python scripts/sync_harmony_assets.py
source /opt/harmonyos-env.sh
cd harmony && /opt/command-line-tools-for-hmos/bin/hvigorw assembleHap --no-daemon
# → entry/build/default/outputs/default/entry-default-unsigned.hap (168K, unsigned)
```

**Config pins** (required for the headless build):
- `build-profile.json5`: `compatibleSdkVersion` / `targetSdkVersion` = `6.1.1(24)`.
- `modelVersion` = `6.0.0` in BOTH `hvigor/hvigor-config.json5` and root `oh-package.json5`
  (hvigor `ValidateUtil.modelVersionCheck` requires both present, equal, ∈ [5.0.0, current]).
- Profile JSON (`accessibility_config.json` etc.) must be strict JSON — **no `//` comments**.
- Needs `$media:app_icon` (a placeholder PNG is committed).
- Output is **unsigned** (no `signingConfigs`): signing + `hdc install` needs a dev cert (DevEco or
  `hap-sign-tool.jar`, `JAVA_HOME=/opt/jdk-17`).

### Contract alignment pins (keep the two runtimes from drifting)

- `JSONAction`: declaration order == `modelDump()` order; `index` vs `(x,y)` mutually exclusive;
  `direction` enum; `keycode` must start with `KEYCODE_`; `equals` is case-insensitive for
  `app_name`/`text` (pinned on the Python side by `tests/test_action_model.py`).
- `NativeEnv`: `scroll` reversal (up↔down, left/right unchanged); swipe geometry `unit=W/10*2`;
  `skip_screenshot` reuses the last frame.
- `UINode`: field mapping + `center` (null when bounds absent or zero/negative area).
- `runTask`: terminal types `{finished, unknown, error_env}`; `ask_user` null = take-over = success
  terminal; `answer` executes then breaks.
- Manifest card `swipe` direction is written as scroll/content-movement direction, reversed at dispatch.

---

## Status

- ✅ host `harmony.py` implemented + 13 unit tests (144 total green); factory dispatch; manifest
  validation no regression.
- ✅ `harmony/` ArkTS app builds an unsigned HAP headless; `sync_harmony_assets.py` 10/10.
- ⏳ On-device behavior (gestures/capture/overlay/one single-app task) + hdc command tokens = device Spike.
- ⏳ Phase 2/3/4 deferred.

All "verify on device" and "not done" items: [`harmony_app_todo.md`](harmony_app_todo.md).
