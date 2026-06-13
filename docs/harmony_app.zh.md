# HarmonyOS App 实现

English: [`harmony_app.md`](harmony_app.md) · TODO / 真机待验证清单：[`harmony_app_todo.md`](harmony_app_todo.md)

把 RelayAgent 装进独立 HarmonyOS NEXT App，与 Android 版（`android/`）对位。两个关键决定：

1. **端侧 runtime 用 ArkTS 重写**（不复用 `agents/` Python）。HarmonyOS NEXT 是 ArkTS/ArkUI，
   **无 Chaquopy / CPython 嵌入**，用平台原生语言重写是 idiomatic 选择。代价：runtime 出现
   双份逻辑（Python host + ArkTS 端侧），靠「契约对齐钉死点」防漂移。
2. **host 侧 `agents/device/harmony.py` 用 hdc/uitest 实现**，与已实现的 `android.py` 对位，
   填掉原来的 `NotImplementedError` 骨架。

本次交付范围：**host `harmony.py` + 端侧最小单 App 循环**（`obs→predict→execute`）。完整
VLM grounding 与 NL 跨 App flow 全量端口为 Phase 2/3，见 TODO 清单。

---

## Part A — host `agents/device/harmony.py`（hdc/uitest）

逐方法镜像 [`agents/device/android.py`](../agents/device/android.py)：adb→hdc，uiautomator-XML→
`uitest dumpLayout` JSON。设备选择用 hdc 的 `-t <connectKey>`（adb 是 `-s`），序列号读
`RELAY_HARMONY_SERIAL`。

| DeviceBackend 方法 | hdc 命令 |
| --- | --- |
| `screencap` | `uitest screenCap -p <path>` + `hdc file recv` → PIL |
| `screen_size` | `hidumper -s RenderService`（缓存 + fallback 1080×2400）|
| `dump_ui_tree` | `uitest dumpLayout -p <path>`(JSON) → `_layout_to_nodes` → `UINode[]`，timeout 参数化 |
| `foreground_app` | `aa dump -a`（正则抽 bundle）|
| `tap` / `long_press` / `swipe_gesture` | `uitest uiInput click / longClick / swipe` |
| `key` | `uitest uiInput keyEvent Back / Home / 2054` |
| `input_text` | `uitest uiInput inputText "<text>"`（无 IME，CJK 直发）|
| `launch` / `force_stop` | `aa start -b <bundle>[ -a <ability>]` / `aa force-stop <bundle>` |
| `cold_launch` | force_stop + launch + settle（照搬 android）|
| `kill_all_apps` | `bm dump -a` ∩ `ps -A` → force-stop 循环 + HOME |
| `setup/teardown_input_channel` | no-op 返回 True（uiInput 不需 IME swap）|
| `start_recording` | 未接线（Phase 4），返回 None + info 日志（对位 iOS gap）|
| `dismiss_permission_popup` | 复用 `vendor_profiles` 的 Allow 标签策略（弹窗在 dumpLayout 内）|

实现要点：
- `hdc_base()` / `_run(args, timeout, text)` 与 `android._run` 同构的 subprocess 封装。
- `_layout_to_nodes(json)` 纯函数：递归 walk dumpLayout 的 `{attributes, children}` 树，文档序产
  `UINode[]`；字段映射隔离在一处（`text`/`description`/`id|key`/`type`/`bundleName`/`bounds`/flags），
  bounds 复用 `[x1,y1][x2,y2]` 正则。可无设备单测。
- `_KEYNAMES = {BACK:"Back", HOME:"Home", ENTER:"2054"}`。
- 日志级别与 android 对齐：observe 失败 warning / dump 解析 info / fallback miss info|warning。

**接线**：[`factory.py`](../agents/device/factory.py) 在 `RELAY_PLATFORM=harmonyos`（`harmony` 归一到
`harmonyos`）构建 `HarmonyBackend(serial=RELAY_HARMONY_SERIAL)`。

**单测**（无设备）：[`tests/test_harmony_backend.py`](../tests/test_harmony_backend.py) — mock
`agents.device.harmony.subprocess.run`，钉 argv 构造（`-t` 注入）、JSON→UINode 解析、超时/解析错误、
`screen_size`/`input_channel` 缓存、`foreground_app`、`launch` bundle/ability 拆分、factory dispatch。
`test_device_backend.py` 同步更新（HarmonyBackend 不再是 NotImplementedError 桩）。

---

## Part B — `harmony/` ArkTS app（最小单 App 循环）

DevEco/hvigor 工程，结构对位 `android/`。端侧 runtime 全 ArkTS，无 Python/Chaquopy/NAPI。

### 文件对应表

| Android（Kotlin + Chaquopy）| HarmonyOS（ArkTS）|
| --- | --- |
| `RelayAccessibilityService.kt` | `ets/device/RelayAccessibilityExtension.ets`（`AccessibilityExtensionAbility`）|
| `DeviceBridge.kt` | `ets/device/DeviceBridge.ets`（同进程 ArkTS 门面）|
| `ScreenCaptureService.kt`（MediaProjection）| `ets/device/ScreenCapture.ets`（AVScreenCapture，**Phase 2**）|
| `OverlayController.kt`（SYSTEM_ALERT_WINDOW）| `ets/device/OverlayController.ets`（`@ohos.window` 浮窗）|
| `SettingsActivity.kt`（EncryptedSharedPreferences）| `ets/settings/SettingsStore.ets`（`@ohos.data.preferences`）|
| `MainActivity.kt` | `ets/entryability/EntryAbility.ets` + `ets/pages/Index.ets` |
| `AssetInstaller.kt` | `ets/device/AssetInstaller.ets`（直读 rawfile）|
| `A11yXmlSerializer.kt`（→ XML）| 直接产 `UINode[]`（`RelayAccessibilityExtension.walk`）|
| Chaquopy 内嵌 `agents/` | `ets/relay/*`（ArkTS 重写）|

### ArkTS runtime（`ets/relay/`，对位 `agents/`）

| ArkTS | 对位 Python | 说明 |
| --- | --- | --- |
| `device.ets` | `device/base.py` + `relay_android/backend.py` | `DeviceBackend` 接口 + `UINode`(含 `center`) + `Key` + `OnDeviceHarmonyBackend`（走 DeviceBridge，async）|
| `actionModel.ets` | `action_model.py` | `JSONAction` + action-type 常量；字段顺序/validator(index↔xy 互斥)/`modelDump`/`equals` 对齐 |
| `nativeEnv.ets` | `native_runtime.py` | `NativeEnv` 派发（scroll **方向反转** + swipe 几何 `unit=屏宽/10*2` + `skip_screenshot`）+ `runTask` 循环 + `Observation` |
| `interaction.ets` | `interaction.py` + `relay_android/interaction.py` | `InteractionProvider`；`askUser` 返回 null=接管=成功终止 |
| `llmClient.ets` | `llm_client.py`(HttpChatClient) | `@ohos.net.http` POST `/chat/completions`，无 streaming，响应归一为 typed interface |
| `agent.ets` | `relay_agent.py`+`action_planner.py`+`capability_router.py` 子集 | `predict`：首帧 routeCapability(1 LLM) + buildPlan，逐步 materialize |
| `nativeRunner.ets` | `native_runner.py:run_leg` | 单 App 入口 `runLeg(card, goal, cfg)` |

**最小循环只支持 deterministic + a11y 步**：`open_app` / `tap_text`(只走 a11y 树，无 VLM) /
`input_text` / `wait_for_reply`(文本-hash 判稳，无 VLM)。card 由 host 脚本转成的 JSON 提供。

### 数据资产（构建期同步）

ArkTS 无 YAML 解析，[`scripts/sync_harmony_assets.py`](../scripts/sync_harmony_assets.py) 把
`manifests/*.yaml` 转成 slim JSON 卡片（`{app_id, app_name, agent_name, capabilities:[{id,
description, input_hint}]}`）+ 拷 capability matrix CSV 到
`harmony/entry/src/main/resources/rawfile/relay/`（`.gitignore` 忽略，构建前生成）。

### 构建（headless，已跑通）

CLT 在 `/opt/command-line-tools-for-hmos`（HarmonyOS 6.1.1 / API 24 / hvigor 6.24.2，见
`/opt/harmonyos-env.sh`）：

```bash
uv run python scripts/sync_harmony_assets.py
source /opt/harmonyos-env.sh
cd harmony && /opt/command-line-tools-for-hmos/bin/hvigorw assembleHap --no-daemon
# → entry/build/default/outputs/default/entry-default-unsigned.hap （168K，unsigned）
```

**配置钉死点**（headless 构建必须）：
- `build-profile.json5`：`compatibleSdkVersion` / `targetSdkVersion` = `6.1.1(24)`。
- `modelVersion` = `6.0.0`，**同时**写在 `hvigor/hvigor-config.json5` 和根 `oh-package.json5`
  （hvigor `ValidateUtil.modelVersionCheck` 要求两处一致且 ∈ [5.0.0, 当前]）。
- profile JSON（`accessibility_config.json` 等）须严格 JSON，**不能有 `//` 注释**。
- 需 `$media:app_icon`（已放占位 PNG）。
- 产物 **unsigned**（未配 `signingConfigs`）：装机前需开发者证书签名（DevEco 或
  `hap-sign-tool.jar`，`JAVA_HOME=/opt/jdk-17`）再 `hdc install`。

### 契约对齐钉死点（双 runtime 不漂移）

ArkTS 端口逐项对齐 host Python（README + 测试钉）：
- `JSONAction`：字段声明顺序 == `modelDump()` 顺序；`index` 与 `(x,y)` 互斥；`direction` 枚举；
  `keycode` 须 `KEYCODE_` 前缀；`equals` 对 `app_name`/`text` 大小写不敏感（Python 侧由
  `tests/test_action_model.py` 钉）。
- `NativeEnv`：`scroll` 方向反转（up↔down，left/right 不变）；swipe 几何 `unit=屏宽/10*2`；
  `skip_screenshot` 复用上一帧。
- `UINode`：字段映射 + `center`（bounds 缺失或零/负面积 → null）。
- `runTask`：terminal 类型 `{finished, unknown, error_env}`；`ask_user` 返回 null = 接管 = 成功终止；
  `answer` 执行后 break。
- manifest 卡片 `swipe` 方向语义：按 scroll / 内容移动方向写，派发时反转。

---

## 状态

- ✅ host `harmony.py` 实现 + 13 条单测（全套 144 全绿）；factory 分发；manifest 校验无回归。
- ✅ `harmony/` ArkTS app headless 构出 unsigned HAP；`sync_harmony_assets.py` 10/10。
- ⏳ 端侧运行行为（无障碍手势/截屏/浮窗/跑通一次单 App 任务）+ hdc 命令 token = 真机 Spike。
- ⏳ Phase 2/3/4 deferred。

全部「真机待验证」与「未做」项见 [`harmony_app_todo.md`](harmony_app_todo.md)。
