<h1 align="center">设备后端</h1>

<p align="center">
  <b>所有设备 I/O 收口到一个缝合层 agents/device/：Android（adb）已实现，iOS / HarmonyOS 为骨架</b>
</p>

<p align="center">
  <a href="device_backends.md">English</a> | <b>中文</b>
</p>

所有设备 I/O 收口到一个缝合层：**`agents/device/`**。上层（runner 循环、
RelayAgent、scripts）不再直接拼 adb/WDA/hdc 命令。

```
agents/device/
├── base.py            DeviceBackend 抽象基类 + UINode + Key
├── android.py         AndroidBackend — adb（已实现，目前唯一真实后端）
├── ios.py             IOSBackend — WebDriverAgent 骨架（NotImplementedError）
├── harmony.py         HarmonyBackend — hdc/uitest 骨架（NotImplementedError）
├── factory.py         get_backend()：RELAY_PLATFORM → 后端实例
└── vendor_profiles.py Android 厂商表（+ RELAY_VENDOR_PROFILE overlay）
```

`agents/runtime/_adb.py` 保留为模块级 shim，委托默认后端——旧 import 面
（`from agents.runtime._adb import screencap`）继续可用，调用方逐步迁移。新代码
直接持实例：

```python
from agents.device import get_backend
backend = get_backend()
```

## 🎛️ 平台与设备选择

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `RELAY_PLATFORM` | `android` | 工厂构建哪个后端（`android` / `ios` / `harmonyos`）。同时作用于 manifest 加载：`platforms` 不含当前平台的卡被跳过。 |
| `RELAY_ANDROID_SERIAL` | 未设 | adb `-s` 序列号，后端创建时读一次。一进程驱动一台设备；每条 leg 子进程继承各自 env。进程内多设备直接构造 `AndroidBackend(serial=...)`。 |
| `RELAY_VENDOR_PROFILE` | 未设 | JSON overlay 路径，扩充厂商权限控制器包名 / Allow 标签（见 `vendor_profiles.py`）。 |
| `RELAY_CROP_TOP` / `RELAY_CROP_BOTTOM` | `0.08` / `0.18` | 状态栏 / 输入栏裁剪比例，reply scrape 与截图区域 hash 共用。 |

## 🌳 归一化 a11y 树：`UINode`

`backend.dump_ui_tree()` 返回文档序的扁平 `list[UINode]` —— 消费者
（grounding、文本 hash、回复 scrape、权限弹窗、`a11y_agent.serialize_tree`）
不再接触 uiautomator XML / WDA page source。

| UINode 字段 | Android 来源 | iOS 来源（规划） |
| --- | --- | --- |
| `text` | `text` | `value` |
| `desc` | `content-desc` | `label` |
| `resource_id` | `resource-id` | accessibility identifier（`name`） |
| `class_name` | `class` | `type` |
| `bounds` | `bounds "[x1,y1][x2,y2]"` | `rect {x,y,width,height}` |
| `clickable` 等 flags | XML 属性 | 由 `type` + `enabled` 推导 |

`UINode.center` 是 tap 点；bounds 缺失或零面积时返回 `None`
（消费者以此作为可见性过滤）。

## 🗺️ 能力映射

| DeviceBackend | Android (adb) | iOS (WebDriverAgent) | HarmonyOS NEXT (hdc) |
| --- | --- | --- | --- |
| `screencap` | `exec-out screencap -p` | `GET /screenshot` | `uitest screenCap` + `hdc file recv` |
| `screen_size` | `wm size` | `GET /window/size` | `hidumper` / `uitest`（待验证） |
| `dump_ui_tree` | `uiautomator dump` → UINode | `GET /source?format=json` → UINode | `uitest dumpLayout` → UINode |
| `foreground_app` | `dumpsys window` → `dumpsys activity` | `GET /wda/activeAppInfo` | `aa dump -a` |
| `tap` / `long_press` | `input tap` / 同点 swipe | `POST /wda/touch/perform` | `uitest uiInput click / longClick` |
| `swipe_gesture` | `input swipe` | `POST /wda/dragfromtoforduration` | `uitest uiInput swipe` |
| `key` BACK / HOME / ENTER | `KEYCODE_*` | 边缘滑动 / `/wda/homescreen` / `/wda/keys "\n"` | `uiInput keyEvent Back / Home` |
| `input_text` | AdbKeyboard `ADB_INPUT_B64` 广播；ASCII 降级 `input text` | `POST /wda/keys`（无 IME 依赖——中文直接发） | `uiInput inputText`（中文待验证） |
| `launch` / `cold_launch` / `force_stop` | monkey LAUNCHER / `am force-stop` | `POST /session {bundleId}` / terminate | `aa start -b` / `aa force-stop` |
| `kill_all_apps` | `pm list -3` ∩ `ps -A` → force-stop | 无法枚举——仅 terminate 已知 id | `bm dump -a` + `aa force-stop` |
| `setup_input_channel` | `ime enable/set` AdbKeyboard | no-op | 预计 no-op（待验证） |
| `start_recording` | `screenrecord` 分段 + pull | mjpeg 端口；真机文件录屏是 **gap** | `uitest record`（待验证） |
| `dismiss_permission_popup` | 厂商包名 + Allow 标签 | springboard alerts：`/alert/text` + `/alert/accept` | 弹窗在 dumpLayout 内，同标签策略 |

## 🍎 iOS 前置（实装时）

Mac + Xcode（用开发者账号构建并签名 WebDriverAgent）+ 打开开发者模式的
iPhone。WDA 以 XCUITest 形态跑在手机上，后端连它的 HTTP 端点（USB 转发
或 Wi-Fi）。相对 Android 的两个已知 gap：无系统返回键（用边缘滑动模拟）、
无 `screenrecord` 等价的端上文件录屏。manifest 须按各 App 的 iOS 版重新
编写（`app_ids.ios` + 可移植 selector——优先 `accessibility_id`/`text`
而非 `resource_id`；声明 ios 的卡若 selector 只有 Android 专属字段，
`scripts/validate/validate_manifests.py` 会 WARN）。

## 🔀 多设备

隔离边界是每条 leg/task 的子进程：驱动方把 `RELAY_ANDROID_SERIAL` 写进
子进程 env（`flow_runner`/`run_benchmark_test` 本来就这么传 per-run 配
置）。`run_benchmark_test` 的设备池（`--serials` + 线程池、父进程 helper
显式收 backend 参数）作为独立改动排期；注意 A/B 公平性要求同一 task 的
两个 system 钉在同一台设备上。
