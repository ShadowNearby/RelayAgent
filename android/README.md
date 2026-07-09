<h1 align="center">RelayAgent Android App</h1>

<p align="center">
  <b>Phase 1 骨架：纯无障碍方案 + Chaquopy 内嵌 CPython——不需要电脑、不需要 adb</b>
</p>

<p align="center">
  <a href="README.en.md">English</a> | <b>中文</b>
</p>

把 RelayAgent 运行时装进独立 Android App：**纯无障碍方案**（AccessibilityService 手势注入 + uiautomator 格式 a11y 树序列化 + MediaProjection 截屏），**Chaquopy 内嵌 CPython** 原样复用仓库的 `agents/`（构建期 `syncRelayPython` 同步），不需要电脑、不需要 adb。

## 🧱 架构对应关系

| 主机（Python over adb） | App 内 |
| --- | --- |
| `adb exec-out screencap`（~1.5s/帧） | MediaProjection VirtualDisplay（~50ms/帧）；a11y `takeScreenshot` 兜底 |
| `uiautomator dump` XML | `A11yXmlSerializer`（uiautomator 格式，Python 解析零改动） |
| `input tap/swipe` | `dispatchGesture` |
| AdbKeyboard 广播输入 | a11y `ACTION_SET_TEXT` → 剪贴板 `ACTION_PASTE` 兜底 |
| BACK/HOME/ENTER | `performGlobalAction` / `ACTION_IME_ENTER` |
| `monkey` 冷启动 | launch intent + `FLAG_ACTIVITY_CLEAR_TASK`（**无真 force-stop，已知语义漂移**） |
| `.env` | 设置页 → EncryptedSharedPreferences → entry 装入 env |
| 终端 ask_user / EOF | 悬浮面板 回答 / 我来接管（=None=handoff 成功终止） |
| 子进程 per leg | `InProcessLegExecutor`（`RELAY_LEG_EXECUTOR=inprocess`） |
| MobileWorld 兜底 | 换成**通用兜底**：`mw_fallback=False` + `allow_mw_legs=False` + `RELAY_MW_FALLBACK=0`，覆盖外 leg / 恢复梯子最后一档改走 `type: general` leg——无 manifest 的 `GeneralGUIAgent`（a11y 树逐步驱动，in-process，`RELAY_GENERAL_MAX_STEP` 限步）；`RELAY_GENERAL_FALLBACK=0`（设置页可覆盖）才回到「无法处理」 |
| `~/.relayagent/profile.yaml`（P3 记忆层） | `RELAY_PROFILE_ROOT=<filesDir>/profile`；M3 写入前的 y/n 走悬浮面板 ask_user；`RELAY_PROFILE`/`RELAY_TRAJ_REDACT`/`RELAY_RECOVERY` 可由设置页经 `_PASSTHROUGH_ENV` 覆盖 |
| scrcpy 流式抓帧 + settle 检测（P2） | 抓帧本就走 MediaProjection ~50ms/帧；settle 检测已对齐——`OnDeviceAndroidBackend.wait_settled` 轮询 `DeviceBridge.captureFrameSeq()`（VirtualDisplay 只在画面变化收帧，与 scrcpy 同性质），quiet 窗口内无新帧即安定；`RELAY_SETTLE_DETECT=0` 或投影断开时回退固定 sleep |

## 🛠️ 构建（需要 Android Studio 的机器）

1. Android Studio 打开 `android/`（首次会自动装 SDK 34 / Gradle wrapper）。
2. 版本核对（**Spike A 的一部分**）：`app/build.gradle.kts` 里 Chaquopy 16.0.0 + Python 3.12 —— 若该版 Chaquopy 不支持 3.12，把 `version` 降到 `"3.11"`（`agents/` 代码 3.10+ 语法兼容；宿主仓库 `requires-python` 不受影响）。
3. `./gradlew :app:assembleDebug`，装到 arm64 真机（minSdk 30 / Android 11+）或 x86_64 模拟器（debug 的 `abiFilters` 含 `x86_64`，正式发布可去掉减体积）。headless 构建需 `JAVA_HOME=/snap/android-studio/current/jbr ANDROID_HOME=~/Android/Sdk`。

## 🧪 真机 instrumented 测试（`app/src/androidTest/`）

在连接的设备（真机或 AVD）上、**App 自身进程内**跑，不需要开无障碍服务 / 屏幕采集授权 / LLM key：

- `PythonRuntimeTest` — 端侧 CPython 启动；`entry.run_flow` 的 import 链（agents.* + relay_android.*）全部可 import；`JSONAction` 行为与宿主 `tests/test_action_model.py` 钉死的一致；从 filesDir/relay 装出的 manifests + capability matrix 能 `build_catalog`/`load_matrix` 且 app_id 相互吻合；Python 侧经 `jclass(DeviceBridge)` 读到的 filesDir 与 Kotlin 一致。
- `AssetInstallerTest` — 资产解包到 filesDir/relay（manifests/*.yaml + matrix CSV），同版本重装是 no-op。
- `TrajLogTest` — 合成一个 run 目录树验证 run/leg/step 解析；坏 JSON 按文档承诺优雅降级。
- `SettingsConfigTest` — `loadConfig` 经 EncryptedSharedPreferences（真机 Keystore）往返；toggle 默认 ON、空字段保持空。**测前备份、测后还原**碰到的 key，不会抹掉设备上已配置的网关。
- `RunEventsTest` — `emit_status` JSON → 主线程典型事件；未知/坏 payload 丢弃。
- `MainActivitySmokeTest` — 会话式主页（composer/建议 chips/气泡渲染）+ Examples/Log/Settings 各页启动 smoke。**刻意不点「发送」**：那条路径需要无障碍服务并会弹 MediaProjection 授权。

跑法（避免 `connectedAndroidTest`——它跑完会卸载 APK、连带清掉 App 数据）：

```bash
./gradlew :app:assembleDebug :app:assembleDebugAndroidTest
adb install -r -t app/build/intermediates/apk/debug/app-debug.apk
adb install -r -t app/build/intermediates/apk/androidTest/debug/app-debug-androidTest.apk
adb shell am instrument -w com.relayagent.app.test/androidx.test.runner.AndroidJUnitRunner
```

注意：espresso-core 须 ≥3.7（旧版在 Android 15+ 反射已删除的 `InputManager.getInstance`，5 条 UI 测试会全挂）。

## 📋 Spike 清单（按序验证，详见总计划 §风险）

- **Spike A（Chaquopy 可行性）**：App 启动 → Python 起来 → `import agents.agent.action_model, yaml, PIL, loguru` 成功 → 设置页填网关 → 跑一次 `relay_android.entry` 里的 LLM 直连（`RELAY_LLM_HTTP=1` 走 stdlib HTTP shim）。
- **Spike A2（后台启动豁免）**：App 退到后台时 `DeviceBridge.launchApp("com.aliyun.tongyi")` 能把千问拉到前台（a11y 上下文 + 前台服务 + SYSTEM_ALERT_WINDOW）。
- **Spike B（dump 保真度）**：同一屏幕分别取 `adb shell uiautomator dump` 与 App 内 `uiDumpXml()`，宿主脚本 diff (text, content-desc, bounds) 节点集；迭代 `A11yXmlSerializer` 直到回复相关节点对齐。

## 🎨 App 界面

Material 3 主题（`res/values/themes.xml` + `colors.xml`，品牌色靛紫），viewBinding：

- **主页 `MainActivity`（会话式，2026-07 改版，对标 Codex/Claude App）**：整屏是一条任务对话流（`RecyclerView` + `ChatThread.kt`）——用户任务是右侧气泡（`item_chat_user`），运行过程是实时活动卡（`item_chat_working`：spinner + 每条子任务一行 ▸/✓ + 当前步骤行，由 `RunEvents` 喂），结果是左侧结果卡（`item_chat_answer`：成败标注 + 回复文本 + 「查看运行详情」跳 `RunDetailActivity`）。底部常驻胶囊输入栏，运行中发送键变停止键；空线程显示问候语 + 3 条示例建议（读 `res/raw/examples.json`）。无障碍 / 网关未就绪时顶部横幅提示（点「去开启」直达设置），历史任务 / 任务示例 / 设置收进 toolbar（`menu/main.xml`）。对话线程存内存单例 `ChatStore`（同 `RunLog` 模式），持久记录仍在轨迹日志查看器。
- **`RunEvents.kt`**：`emit_status` JSON → 类型化事件总线（`LegStart`/`Step`/`LegEnd`/`AskUser`/`AskAnswered`），`OverlayController.postStatus` 分发（悬浮 chip / RunLog / 对话流三路同源）；`DeviceBridge.askUser` 阻塞前后补发 ask 事件，线程里能看到「等待你的回答」。
- **任务示例 `ExamplesActivity`**：读 `res/raw/examples.json`（50 条：30 RelayBench + 20 AndroidDaily，由 `scripts/android/gen_app_examples.py` 从 `benchmark/` 生成），卡片 + 标签（来源 / App / 类别 / 难度），点按回填任务框。改基准后重跑脚本即可刷新。
- **运行日志（结构化查看器）**：三级——`LogActivity`（运行列表：任务原文 / 时间 / App 标签 / 子任务数，新→旧；溢出菜单可**清除全部日志**，列表顶部有截图隐私提示）→ `RunDetailActivity`（一次运行的任务卡 + 各子任务卡：状态徽章 / 步数 / 墙钟 / token / 回复预览）→ `LegDetailActivity`（步骤时间线：每步标注截图缩略图 + action 类型 + 坐标/参数 + thought，点缩略图全屏看帧）。解析在 `TrajLog.kt`（吃 `meta.json` / `summary.json` / `wall_clock.json` / `agent_reply.json` / `leg_verdict.json` / `steps/steps.json`，缺字段优雅降级），App 名映射在 `AppLabels.kt`。原始文件树退到 toolbar 溢出菜单「查看原始文件」→ `RawLogActivity`（+ `LogDetailActivity` 渲染单文件：JSON 美化 / PNG 进 ImageView）。`entry.py` 落 `meta.json`（任务原文 + kind），查看器才能显示「这是什么任务」。主页实时日志卡只是当次运行的 tail。
- **实时日志**：`RunLog` 单例滚动缓冲。`OverlayController.postStatus` 把 `emit_status` 事件（`leg_start` / `leg_end` / `step`）格式成中文短行，同时喂悬浮 chip 和主页日志卡。悬浮 chip / ask_user 面板用圆角 drawable。

## 🔌 接线状态

- **`agents.device` 注入缝已落地并接线**：`relay_android/backend.py:install()` 经 `set_default_backend` 注入 `OnDeviceAndroidBackend`，已在模拟器上跑通（CPython 启动 → backend 注入 → MediaProjection 截帧 → 三段式路由 → flow 规划 → in-process leg 执行 → traj/wall_clock 落 filesDir）。模拟器搭建与 APK 安装步骤见 [`../docs/emulator_testing.zh.md`](../docs/emulator_testing.zh.md) §7。
- 主机侧复用件：`run_leg` 进程内执行、`InProcessLegExecutor`、`InteractionProvider`、`make_llm_client` HTTP shim、`nl_flow.plan_request/execute_plan`、`RELAY_TRAJ_ROOT` 重定向。`native_runner._agent_spec` 在磁盘无 `agents/agent/relay_agent.py` 时（Chaquopy AssetFinder 打包形态）回落包内 module spec 加载 agent。

## 🔒 安全与隐私

- **不可逆动作护栏**：卡片能力经 `handoff_to_user_required` + `stop_before` 在支付/下单等不可逆 CTA 前**停下交还用户**；通用兜底 agent（`GeneralGUIAgent`）带中英双语 CTA 停止表（支付/转账/下单/订房等），命中即转 ask_user，**永不自行跨越**。
- **系统权限弹窗自动点击**：运行中目标 App 弹出的系统权限对话框（相机/定位/麦克风…）默认自动点**最宽松的允许**（永不点拒绝；仅当前台是已知权限控制器包名才触发，每任务上限 8 次）。这意味着 agent 会替你授予目标 App 请求的运行时权限——不接受就在**设置页关掉「自动允许权限弹窗」**（= `RELAY_DISMISS_PERMISSIONS=0`），弹窗将留在屏上等你手点。
- **运行日志含原始截图**：`traj_logs/` 里每步落屏幕帧（聊天内容、余额、地址都可能在内）。分享排障日志前先过一遍;`RELAY_TRAJ_REDACT=1` 只替换 profile 文本值,**不会**擦除截图像素。
- **用户画像本地存储**：`filesDir/profile/profile.yaml`,只在本机,随 App 数据清除;写入前必经 y/n 确认。

## 📁 运行时数据布局（filesDir）

```
files/
  relay/manifests/*.yaml          # AssetInstaller 解包（构建期从仓库同步）
  relay/app_capability_matrix.csv
  relay/_generated/               # plan 缓存（与主机 manifests/_generated 同构）
  traj_logs/<ts>_plan_<apps>/NN_<id>/{traj.json,steps/,...}   # 与主机同构
```
