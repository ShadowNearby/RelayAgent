# RelayAgent HarmonyOS App（Phase 1 骨架）

> 架构 / 实现的权威文档：[`../docs/harmony_app.zh.md`](../docs/harmony_app.zh.md)（English:
> [`../docs/harmony_app.md`](../docs/harmony_app.md)）；TODO / 真机待验证清单：
> [`../docs/harmony_app_todo.md`](../docs/harmony_app_todo.md)。本 README 是构建快速上手。

把 RelayAgent 运行时装进独立 HarmonyOS NEXT App：**纯无障碍方案**
（`AccessibilityExtensionAbility` 注入手势 + `getWindowRootElement` 序列化 a11y 树）。
与 Android 版关键差异：HarmonyOS NEXT **没有 Chaquopy / CPython 嵌入**，所以端侧
runtime 用 **ArkTS 重写**（不复用 `agents/` Python）。host 侧 `agents/device/harmony.py`
（hdc/uitest）是同一套 DeviceBackend 契约的 Python 实现，供有电脑时驱动；本 App 是无电脑、
无 hdc 的端上形态。

> 本次只交付 **最小单 App 循环**（`obs→predict→execute`）。完整 VLM grounding 与
> NL 跨 App flow 端口列为 Phase 2/3，见下「未做」。

## 架构对应关系（对位 `android/`）

| Android（Kotlin + Chaquopy CPython） | HarmonyOS（ArkTS） |
| --- | --- |
| `DeviceBridge.kt` | `ets/device/DeviceBridge.ets` |
| `RelayAccessibilityService.kt` | `ets/device/RelayAccessibilityExtension.ets`（`AccessibilityExtensionAbility`）|
| `ScreenCaptureService.kt`（MediaProjection）| `ets/device/ScreenCapture.ets`（AVScreenCapture，**Phase 2**）|
| `OverlayController.kt`（SYSTEM_ALERT_WINDOW）| `ets/device/OverlayController.ets`（`@ohos.window` 浮窗）|
| `SettingsActivity.kt`（EncryptedSharedPreferences）| `ets/settings/SettingsStore.ets`（`@ohos.data.preferences`）|
| `MainActivity.kt` | `ets/entryability/EntryAbility.ets` + `ets/pages/Index.ets` |
| `AssetInstaller.kt` | `ets/device/AssetInstaller.ets`（直读 rawfile）|
| `A11yXmlSerializer.kt`（→ uiautomator XML）| 直接产 `UINode[]`（`RelayAccessibilityExtension.walk`）|
| Chaquopy 内嵌 `agents/` | `ets/relay/*`（ArkTS 重写）|

### ArkTS runtime（`ets/relay/`，对位 `agents/`）

| ArkTS | 对位 Python | 说明 |
| --- | --- | --- |
| `device.ets` | `agents/device/base.py` + `relay_android/backend.py` | `DeviceBackend` 接口 + `UINode`(含 `center`) + `Key` + `OnDeviceHarmonyBackend` |
| `actionModel.ets` | `agents/action_model.py` | `JSONAction` + action-type 常量；字段顺序/validator/`modelDump`/`equals` 与 Python 对齐 |
| `nativeEnv.ets` | `agents/native_runtime.py` | `NativeEnv` 派发（scroll **方向反转** + swipe 几何 + `skip_screenshot`）+ `runTask` 循环 + `Observation` |
| `interaction.ets` | `agents/interaction.py` + `relay_android/interaction.py` | `InteractionProvider`；`askUser` 返回 null=接管=成功终止 |
| `llmClient.ets` | `agents/llm_client.py`（HttpChatClient）| `@ohos.net.http` POST `/chat/completions`，无 streaming，响应归一 |
| `agent.ets` | `relay_agent.py`+`action_planner.py`+`capability_router.py` 的**子集** | `predict`：首帧 routeCapability(1 LLM) + buildPlan，逐步 materialize |
| `nativeRunner.ets` | `agents/native_runner.py:run_leg` | 单 App 入口 |

## 契约对齐（双 runtime 不漂移的钉死点）

ArkTS 端口逐项对齐 host Python 行为：

- `JSONAction`：字段声明顺序 == `modelDump()` 顺序；`index` 与 `(x,y)` 互斥；`direction`
  枚举；`keycode` 须 `KEYCODE_` 前缀；`equals` 对 `app_name`/`text` 大小写不敏感。
  （Python 侧由 `tests/test_action_model.py` 钉死。）
- `NativeEnv`：`scroll` 方向反转（up↔down，left/right 不变）；swipe 几何 `unit = 屏宽/10*2`；
  `skip_screenshot` 复用上一帧。
- `UINode`：字段映射 + `center`（bounds 缺失或零/负面积 → null）。
- `runTask`：terminal 类型集合 `{finished, unknown, error_env}`；`ask_user` 返回 null = 接管 =
  成功终止；`answer` 执行后 break。
- manifest 卡片 `swipe` 方向语义：按 scroll / 内容移动方向写，派发时反转（同主机约定）。

## 数据资产（构建期同步）

ArkTS 无 YAML 解析，所以 host 脚本把 manifests 转成 slim JSON 卡片：

```bash
uv run python scripts/sync_harmony_assets.py     # → entry/.../resources/rawfile/relay/
```

产物（`.gitignore` 忽略，构建前生成）：

```
entry/src/main/resources/rawfile/relay/
  manifests/<app_id>.json     # {app_id, app_name, agent_name, capabilities:[{id, description, input_hint}]}
  app_capability_matrix.csv
```

## 构建（headless，已跑通）

CLT 在 `/opt/command-line-tools-for-hmos`（HarmonyOS 6.1.1 / API 24 / hvigor 6.24.2，
见 `/opt/harmonyos-env.sh`）。无 DevEco IDE 也能 headless 构出 HAP：

```bash
uv run python scripts/sync_harmony_assets.py            # 1. 先同步卡片到 rawfile
source /opt/harmonyos-env.sh                            # 2. 激活 HarmonyOS 工具链
cd harmony && /opt/command-line-tools-for-hmos/bin/hvigorw assembleHap --no-daemon
# → entry/build/default/outputs/default/entry-default-unsigned.hap
```

产物是 **unsigned HAP**（项目未配 `signingConfigs`）。装真机前需用开发者证书签名
（DevEco 或 `hap-sign-tool.jar`，`JAVA_HOME=/opt/jdk-17`），再 `hdc install`。装到真机
（开发者模式 + 无障碍授权）或 DevEco 模拟器。

> 项目配置要点（headless 构建钉死）：`build-profile.json5` 的 `compatibleSdkVersion`/
> `targetSdkVersion` = `6.1.1(24)`；`hvigor/hvigor-config.json5` 与根 `oh-package.json5`
> 的 `modelVersion` 都 = `6.0.0`（hvigor `ValidateUtil.modelVersionCheck` 要求两处一致且
> ∈ [5.0.0, 当前]）；profile JSON（`accessibility_config.json` 等）须严格 JSON 无注释。

## Spike 清单（按序验证，需真机）

- **Spike A（a11y 保真度）**：开启无障碍 → `RelayAccessibilityExtension.dumpUiTree()` 的
  `UINode[]` 与真 `hdc shell uitest dumpLayout` 的 (text/desc/bounds) 节点集 diff；迭代
  `walk()` 的属性 key 映射直到对齐。注：a11y API 已按 `/opt/.../sdk` 的 `@kit.AccessibilityKit`
  d.ts 强类型对齐（`attributeValue` 的 `ElementAttributeValues` key、`getFocusElement`、
  `injectGesture`/`GesturePath`），但运行语义（setText 参数形态、global back/home 缺失）仍待真机核，见
  各文件 `TODO(verify-on-device)`。
- **Spike B（手势 / 启动）**：`injectGesture` 的 tap/swipe 落点正确；后台 `startAbility`
  能把目标 App 拉到前台（浮窗 + 后台启动豁免）。
- **Spike C（端到端）**：设置页填网关 → 跑一次单 App 任务（如千问 `foundation_llm`）→ a11y
  抓回复 → `answer`。

## 已知语义漂移 / 未做（端侧接受）

- **无真 force-stop**（无 shell）：冷启动以 relaunch 近似，目标 App 内存态可能跨 leg 残留。
- **Phase 2**：`ScreenCapture` 帧捕获（AVScreenCapture）→ 解锁 `tap_text` 的 VLM grounding
  与 VLM 读回复；`OverlayController` 浮窗 UI 实体化；端侧权限弹窗自动 dismiss；端上录屏。
- **Phase 3**：NL 跨 App flow 全量端口（`flow_planner` / 三段式 `capability_matrix_router` /
  `nl_flow` 缓存 / `flow_runner` leg 编排 / MW 兜底）。当前入口只跑单 App `runLeg`。
- **Phase 4**：设计版前端 UI。
