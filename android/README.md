# RelayAgent Android App（Phase 1 骨架）

把 RelayAgent 运行时装进独立 Android App：**纯无障碍方案**（AccessibilityService 手势注入 + uiautomator 格式 a11y 树序列化 + MediaProjection 截屏），**Chaquopy 内嵌 CPython** 原样复用仓库的 `agents/`（构建期 `syncRelayPython` 同步），不需要电脑、不需要 adb。

## 架构对应关系

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
| MobileWorld 兜底 | 砍掉：`mw_fallback=False` + `allow_mw_legs=False` → 「无法处理」 |

## 构建（需要 Android Studio 的机器）

1. Android Studio 打开 `android/`（首次会自动装 SDK 34 / Gradle wrapper）。
2. 版本核对（**Spike A 的一部分**）：`app/build.gradle.kts` 里 Chaquopy 16.0.0 + Python 3.12 —— 若该版 Chaquopy 不支持 3.12，把 `version` 降到 `"3.11"`（`agents/` 代码 3.10+ 语法兼容；宿主仓库 `requires-python` 不受影响）。
3. `./gradlew :app:assembleDebug`，装到 arm64 真机（minSdk 30 / Android 11+）。

## Spike 清单（按序验证，详见总计划 §风险）

- **Spike A（Chaquopy 可行性）**：App 启动 → Python 起来 → `import agents.action_model, yaml, PIL, loguru` 成功 → 设置页填网关 → 跑一次 `relay_android.entry` 里的 LLM 直连（`RELAY_LLM_HTTP=1` 走 stdlib HTTP shim）。
- **Spike A2（后台启动豁免）**：App 退到后台时 `DeviceBridge.launchApp("com.aliyun.tongyi")` 能把千问拉到前台（a11y 上下文 + 前台服务 + SYSTEM_ALERT_WINDOW）。
- **Spike B（dump 保真度）**：同一屏幕分别取 `adb shell uiautomator dump` 与 App 内 `uiDumpXml()`，宿主脚本 diff (text, content-desc, bounds) 节点集；迭代 `A11yXmlSerializer` 直到回复相关节点对齐。

## 已知阻塞 / 待接线

- **`relay_android/backend.py` 的 `install()` 依赖 `agents.device` 注入缝**（DeviceBackend 抽象，P0.1，正在 device-backend 分支落地）。落地前 entry 会快速失败并报清晰错误。落地后若接口名有出入，只需改 `AndroidBackend` 的方法名映射。
- 主机侧已就绪的部分：`run_leg` 进程内执行、`InProcessLegExecutor`、`InteractionProvider`、`make_llm_client` HTTP shim、`nl_flow.plan_request/execute_plan`、`RELAY_TRAJ_ROOT` 重定向。

## 运行时数据布局（filesDir）

```
files/
  relay/manifests/*.yaml          # AssetInstaller 解包（构建期从仓库同步）
  relay/app_capability_matrix.csv
  relay/_generated/               # plan 缓存（与主机 manifests/_generated 同构）
  traj_logs/<ts>_plan_<apps>/NN_<id>/{traj.json,steps/,...}   # 与主机同构
```
