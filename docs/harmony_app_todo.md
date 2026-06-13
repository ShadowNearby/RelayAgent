# HarmonyOS App — TODO / 真机待验证清单

实现文档：[`harmony_app.zh.md`](harmony_app.zh.md)（English: [`harmony_app.md`](harmony_app.md)）。

本清单汇总所有 `TODO(verify-on-device)` 标记与 deferred 项。无真机/无开发者证书的环境只能写、
不能验；以下按「需真机核实」「需账号/证书」「Phase 2/3/4 未做」分组。代码里同一处都有内联注释。

---

## A. host `agents/device/harmony.py` — hdc/uitest 命令 token 待真机核

| 位置 | 待核 |
| --- | --- |
| `screencap` | `uitest screenCap -p <path>` 路径形态 + `hdc file recv` 行为 |
| `screen_size` (~L204) | `hidumper -s RenderService` 哪个 section 带物理分辨率；解析正则 |
| `dump_ui_tree` / `_layout_to_nodes` (~L81) | `uitest dumpLayout` JSON 的字段名（`id` vs `key`、`description` vs `content`、`bounds` 形态）与 flag 取值 |
| `foreground_app` (~L268) | `aa dump -a` 前台 bundle 的确切行格式；正则 |
| `swipe_gesture` (~L316) | `uiInput swipe` 是 velocity(px/s) 不是 duration；要精确时序需 duration_ms→velocity 映射 |
| `long_press` (~L301) | `uiInput longClick` 是否吃 duration；不吃则回落同点 swipe |
| `key` ENTER (~L49) | `uiInput keyEvent` 的 Enter token（暂用 keycode `2054`）|
| `input_text` (~L340) | 部分版本 `uiInput inputText` 要 `<x> <y> <text>`（先点聚焦）；CJK 行为 |
| `kill_all_apps` | `bm dump -a` 枚举格式 + `ps -A` 进程名是否=bundle id |
| `launch` | `aa start -b <bundle>` 能否不带 ability 起；默认 ability 名 |

> 改法：命令/解析都隔离在小函数，真机观察后单点修正即可。

## B. 端侧 ArkTS — 运行语义待真机核

| 文件 | 待核 |
| --- | --- |
| `RelayAccessibilityExtension.ets:walk` | `attributeValue` 各 key 与真 `uitest dumpLayout` 节点集对齐（已按 `@kit.AccessibilityKit` d.ts 强类型，但语义需对）|
| `RelayAccessibilityExtension.ets:keyevent` (~L135) | `AccessibilityExtensionContext` 无 global back/home；当前返回 false。ENTER/submit 改用 Send 按钮 tap 或其他 API |
| `RelayAccessibilityExtension.ets:inputText` | `performAction('setText', { text })` 参数键名形态 |
| `DeviceBridge.ets:AppLauncher` (~L92) | `startAbility` 的 bundle→ability 解析（默认 `EntryAbility`）+ 后台启动豁免（浮窗权限）|
| `module.json5` (~L8) | 权限名（`SYSTEM_FLOAT_WINDOW`/截屏/后台）+ `ohos.accessibleability` profile key 是否随 SDK 变 |
| `accessibility_config.json` | `accessibilityCapabilities`/`accessibilityEventTypes` 枚举拼写 |

## C. 需华为账号 / 开发者证书（环境外）

- **HAP 签名**：当前产物 `entry-default-unsigned.hap`。需开发者证书 + profile（`.p12`/`.cer`/`.p7b`），
  在 `build-profile.json5` 配 `signingConfigs`，或命令行 `hap-sign-tool.jar`（`JAVA_HOME=/opt/jdk-17`）。
- **装机**：签名后 `hdc install <hap>` 到真机（开发者模式 + 手动授权无障碍）或 DevEco 模拟器。

## D. Phase 2/3/4 未做（端侧 deferred）

- **Phase 2**
  - `ScreenCapture.ets`：AVScreenCapture → Surface → PixelMap（需用户截屏授权，类比 MediaProjection）。
    解锁 `tap_text` 的 VLM grounding 兜底 与 VLM 读回复。
  - `OverlayController.ets`：浮窗 UI 实体化（`@ohos.window` createWindow + ArkUI 组件）；现为 promise/状态
    plumbing 占位。
  - 端侧权限弹窗自动 dismiss。
  - 端上录屏（`uitest record`）。
- **Phase 3**：NL 跨 App flow 全量 ArkTS 端口 —— `flow_planner` / 三段式 `capability_matrix_router` /
  `nl_flow` 缓存 / `flow_runner` leg 编排 / MW 兜底。当前 app 入口只跑单 App `runLeg`。
- **Phase 4**：设计版前端 UI（现为 programmatic 最小界面）。

## E. Spike 验证（有真机后按序）

- **Spike A（a11y 保真度）**：`dumpUiTree()` 的 `UINode[]` 对比真 `hdc shell uitest dumpLayout`
  的 (text/desc/bounds) 节点集，迭代 `walk()` 直到对齐。
- **Spike B（手势/启动）**：`injectGesture` tap/swipe 落点正确；后台 `startAbility` 能把目标 App 拉前台。
- **Spike C（端到端）**：设置页填网关 → 跑一次单 App 任务（如千问 `foundation_llm`）→ a11y 抓回复 → `answer`。
