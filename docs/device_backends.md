# Device backends — multi-platform abstraction

中文版: [`device_backends.zh.md`](device_backends.zh.md)

All device I/O goes through one seam: **`agents/device/`**. Code above it
(runtime loop, RelayAgent, scripts) never composes adb/WDA/hdc commands
directly.

```
agents/device/
├── base.py            DeviceBackend ABC + UINode + Key
├── android.py         AndroidBackend — adb (IMPLEMENTED, the only real backend)
├── ios.py             IOSBackend — WebDriverAgent skeleton (NotImplementedError)
├── harmony.py         HarmonyBackend — hdc/uitest skeleton (NotImplementedError)
├── factory.py         get_backend(): RELAY_PLATFORM → backend instance
└── vendor_profiles.py Android vendor tables (+ RELAY_VENDOR_PROFILE overlay)
```

`agents/runtime/_adb.py` remains as a module-level shim delegating to the default
backend — the legacy import surface (`from agents.runtime._adb import screencap`)
keeps working while call sites migrate. New code should hold an instance:

```python
from agents.device import get_backend
backend = get_backend()
```

## Selecting platform and device

| Env | Default | Meaning |
| --- | --- | --- |
| `RELAY_PLATFORM` | `android` | Which backend the factory builds (`android` / `ios` / `harmonyos`). Also gates manifest loading: cards whose `platforms` does not include it are skipped. |
| `RELAY_ANDROID_SERIAL` | unset | adb `-s` serial, read once at backend creation. One process drives one device; per-leg subprocesses inherit their own env. In-process multi-device code constructs `AndroidBackend(serial=...)` directly. |
| `RELAY_VENDOR_PROFILE` | unset | Path to a JSON overlay adding vendor permission-controller packages / Allow labels (see `vendor_profiles.py`). |
| `RELAY_CROP_TOP` / `RELAY_CROP_BOTTOM` | `0.08` / `0.18` | Status-bar / input-bar crop ratios used by the reply scrape and the screenshot-region hash. |

## Normalized accessibility tree: `UINode`

`backend.dump_ui_tree()` returns a flat `list[UINode]` in document order —
consumers (grounding, text hash, reply scrape, permission dismiss,
`a11y_agent.serialize_tree`) never see uiautomator XML / WDA page source.

| UINode field | Android source | iOS source (planned) |
| --- | --- | --- |
| `text` | `text` | `value` |
| `desc` | `content-desc` | `label` |
| `resource_id` | `resource-id` | accessibility identifier (`name`) |
| `class_name` | `class` | `type` |
| `bounds` | `bounds "[x1,y1][x2,y2]"` | `rect {x,y,width,height}` |
| `clickable` 等 flags | XML attrs | derived from `type` + `enabled` |

`UINode.center` is the tap point; it returns `None` for absent or
zero-area bounds (consumers rely on this as the visibility filter).

## Capability mapping

| DeviceBackend | Android (adb) | iOS (WebDriverAgent) | HarmonyOS NEXT (hdc) |
| --- | --- | --- | --- |
| `screencap` | `exec-out screencap -p` | `GET /screenshot` | `uitest screenCap` + `hdc file recv` |
| `screen_size` | `wm size` | `GET /window/size` | `hidumper` / `uitest` (TBV) |
| `dump_ui_tree` | `uiautomator dump` → UINode | `GET /source?format=json` → UINode | `uitest dumpLayout` → UINode |
| `foreground_app` | `dumpsys window` → `dumpsys activity` | `GET /wda/activeAppInfo` | `aa dump -a` |
| `tap` / `long_press` | `input tap` / same-point swipe | `POST /wda/touch/perform` | `uitest uiInput click / longClick` |
| `swipe_gesture` | `input swipe` | `POST /wda/dragfromtoforduration` | `uitest uiInput swipe` |
| `key` BACK / HOME / ENTER | `KEYCODE_*` | edge-swipe / `/wda/homescreen` / `/wda/keys "\n"` | `uiInput keyEvent Back / Home` |
| `input_text` | AdbKeyboard `ADB_INPUT_B64` broadcast; ASCII `input text` fallback | `POST /wda/keys` (no IME dependency — CJK works) | `uiInput inputText` (CJK TBV) |
| `launch` / `cold_launch` / `force_stop` | monkey LAUNCHER / `am force-stop` | `POST /session {bundleId}` / terminate | `aa start -b` / `aa force-stop` |
| `kill_all_apps` | `pm list -3` ∩ `ps -A` → force-stop | no enumeration — known ids only | `bm dump -a` + `aa force-stop` |
| `setup_input_channel` | `ime enable/set` AdbKeyboard | no-op | likely no-op (TBV) |
| `start_recording` | `screenrecord` chunked + pull | mjpeg port; real-device file recording is a **gap** | `uitest record` (TBV) |
| `dismiss_permission_popup` | vendor packages + Allow labels | springboard alerts: `/alert/text` + `/alert/accept` | dialogs appear in dumpLayout; same label strategy |

TBV = to be verified on a real device.

## iOS prerequisites (when implementing)

Mac + Xcode (to build & sign WebDriverAgent with a developer account) +
iPhone with developer mode. WDA runs as an XCUITest on the phone; the
backend talks to its HTTP endpoint (USB-forwarded or Wi-Fi). Two known
gaps vs Android: no system back key (edge-swipe substitute), and no
on-device file recording equivalent of `screenrecord`. Manifests must be
re-authored against the iOS build of each app (`app_ids.ios` + portable
selectors — prefer `accessibility_id`/`text` over `resource_id`;
`scripts/validate/validate_manifests.py` warns on Android-only selectors in cards
that declare ios).

## Multi-device runs

Per-leg/task subprocesses are the isolation boundary: drivers put
`RELAY_ANDROID_SERIAL` into each child's env (this is how
`flow_runner`/`run_benchmark_test` already pass per-run config). A device
pool for `run_benchmark_test` (`--serials` + thread pool, parent-process
helpers taking an explicit backend) is planned as a separate change; note
that A/B fairness requires both systems of one task to run on the SAME
device.
