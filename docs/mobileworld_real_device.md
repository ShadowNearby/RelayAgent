<h1 align="center">用 MobileWorld 跑真机测试</h1>

<p align="center">
  <b>用 MobileWorld 的 real-device 模式经 ADB 直驱物理机，执行任意自然语言目标</b>
</p>

> MobileWorld 是本项目 A/B 评测的 baseline（`general_e2e`，见 [`evaluation.zh.md`](evaluation.zh.md)），也是 NL flow 的兜底执行器（见 [`nl_flow.zh.md`](nl_flow.zh.md) §10）。它本体是 Docker 模拟器上的 benchmark（201 个预定义任务 + 评测器），但其 **real-device 模式**可以直接用 ADB 驱动物理机执行任意自然语言目标，无需预先编写 task 类。本文记录这一入口的用法。

## ✅ 前置条件

- 物理 Android 机 USB 连接，已开 USB 调试（`adb devices` 能看到 `device`）。
- 已装 ADB platform-tools。
- MobileWorld 通过以下任一方式提供：
  - 安装成当前 `uv` 环境里的 Python package / console command（`uv run mw ...` 可用）；
  - 作为仓库内 git submodule 放在 `third_party/MobileWorld`。
- AdbKeyboard 用于文本输入（MobileWorld 会自动装；手动：
  `adb install third_party/MobileWorld/ADBKeyboard.apk` 后
  `adb shell ime enable com.android.adbkeyboard/.AdbIME`）。
- 目标 App 已装在设备上。
- 多设备时用 `RELAY_ANDROID_SERIAL` / `ANDROID_SERIAL` 选设备。

## 🔑 凭证（复用 `.env` 的 LLM 配置）

复用 RelayAgent 的 `.env`（**不要提交、不要在命令行明文粘贴 key**）：

| 参数 | 值 |
| --- | --- |
| `--llm_base_url` | `.env` 里的 `LLM_BASE_URL` |
| `--model_name` | `.env` 里的 `LLM_MODEL`（如 `qwen`） |
| `--api_key` | `.env` 里的 `LLM_API_KEY` |
| `--agent-type` | `general_e2e`（qwen-3.5 适用，相对坐标 0–1000） |

## 🚀 步骤

```bash
cd RelayAgent

# 1. 提供 MobileWorld（任选其一）
# A. Python package / console command：确认 `uv run mw --help` 可用
uv run mw --help

# B. git submodule：放在仓库内相对路径
git submodule add https://github.com/ShadowNearby/MobileWorld third_party/MobileWorld
cd third_party/MobileWorld
uv sync
cd ../..

# 2. 跑临时目标（脚本会读取 .env、复用/启动 MobileWorld server、预开目标 app）
uv run python scripts/run_mobileworld.py "Live navigate to the Bund by Google Map"
```

> 旗标连字符/下划线两种写法都收（`--model-name` == `--model_name`）。
> `mw test` 是临时单任务入口，**不做 task 初始化/校验**，直接对当前设备屏幕开跑。
> `--max-round` 限步数（默认 -1 不限），`--timeout` 限墙钟秒数。

脚本默认等价于：

```bash
uv run python scripts/run_mobileworld.py "Live navigate to the Bund by Google Map" \
  --app com.google.android.apps.maps \
  --agent-type general_e2e \
  --model-name qwen \
  --max-round 25 \
  --timeout 600
```

把 key 传入而不落进 shell history 的写法：

```bash
export LLM_API_KEY=$(grep '^LLM_API_KEY=' .env | cut -d= -f2-)
uv run mw test "..." ... --api_key "$LLM_API_KEY"
```

## ⚠️ 关键：先预开目标 App，别让 agent 从桌面找

从**桌面**起跑时，agent 可能反复 `scroll up` 试图打开应用抽屉，而部分机型从桌面上滑会拉出**通知栏**，agent 看到通知栏又尝试"上滑关掉"，形成死循环（实测到 step 9 仍未进入目标 App）。

**解法：跑之前先把目标 App 拉到前台**（脚本的 `--app` 参数会用 `monkey` 预开），agent 从 App 内起步即可稳定执行（同一目标预开后 5 步完成）：

```bash
uv run python scripts/run_mobileworld.py "Live navigate to the Bund by Google Map" \
  --app com.google.android.apps.maps
```

goal 仍写完整意图（"Live navigate to the Bund by Google Map"），agent 会在 App 内搜索并发起导航，不受预开影响。

## 🎥 实时看屏 / 录屏

- 看实时设备画面：`uv run mw device`。
- 录屏默认不开启；加 `--record` 后自动分段 `adb screenrecord`，任务结束时用 `ffmpeg` 合并到 `recording.mp4`：

```bash
uv run python scripts/run_mobileworld.py "Live navigate to the Bund by Google Map" --record
```

默认输出到 `recordings/mobileworld_<timestamp>/recording.mp4`，也可用 `--record-dir <dir>` 指定目录。

> `adb screenrecord` 单段上限 180s，脚本会循环分段录制。若本机没有 `ffmpeg`，会保留 `chunk_*.mp4` 和 `concat.txt`，不做合并。

## 📱 示例

| 项 | 值 |
| --- | --- |
| goal | `Live navigate to the Bund by Google Map` |
| 驱动 | `.env` 配置的 qwen，`general_e2e`，Pixel 9 |
| 起点 | 预开 Google Maps（见上「关键」） |
| 结果 | **5 步**：点搜索框 → 输入 "The Bund" → 选中外滩 → Start → 进入实时逐向导航 |
| 录屏 | `recordings/mw_bund_<ts>/recording.mp4`（1080×2424，约 86s）|

对照：同一目标**不预开、从桌面起跑**时，agent 卡在 `scroll up` 死循环，到 step 9 仍未进入 App——故必须预开。

## 🧭 模型 / 坐标系参考

MobileWorld 仓库 `docs/real-devices.md` 的对照表：

| 模型 | agent-type | 坐标系 |
| --- | --- | --- |
| qwen-3.5 | `general_e2e` | 相对 0–1000 |
| Gemini 3 Pro | `general_e2e` | 相对 0–1000 |
| Claude Opus/Sonnet | `general_e2e` | 绝对像素（Sonnet 需 resize 到 1280×720） |
| Seed-2.0-Pro | `seed_agent` | 相对 0–1000 |

## 📌 注意

- 这是真机直驱，会真实改变设备状态（发起导航、定位等）。导航类目标依赖设备真实 GPS / 网络；在国内需保证 Google 服务可达。
- 模拟器模式（`uv run mobile-world ...` 跑 Docker 快照）可能没有真实 GPS / 实时导航，导航类演示请用 real-device 模式。
- 命令里**不要**硬编码 API key，用 `$LLM_API_KEY` 从 `.env` 注入。
