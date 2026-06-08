# 用 MobileWorld 跑真机测试

记录如何用 **MobileWorld** 通过 ADB 驱动真机，
以 **SJTU IPADS 网关的 qwen** 作为 agent 大脑，跑一个临时目标（如让 Google Maps 导航）。

> MobileWorld 已从 RelayAgent 主仓库移除（见项目记忆「Dropped MobileWorld」），
> 它是一个 Docker 模拟器上的 benchmark（201 个预定义任务 + 评测器）。
> 但它的 **real-device 模式** 可以直接用 ADB 驱动物理机跑**任意自然语言目标**，
> 不需要预先写 task 类——这正是录对比视频要用的入口。

## 前置条件

- 物理 Android 机 USB 连接，已开 USB 调试（`adb devices` 能看到 `device`）。
- 已装 ADB platform-tools。
- MobileWorld 通过以下任一方式提供：
  - 安装成当前 `uv` 环境里的 Python package / console command（`uv run mw ...` 可用）；
  - 作为仓库内 git submodule 放在 `third_party/MobileWorld`。
- AdbKeyboard 用于文本输入（MobileWorld 会自动装；手动：
  `adb install third_party/MobileWorld/ADBKeyboard.apk` 后
  `adb shell ime enable com.android.adbkeyboard/.AdbIME`）。
- 目标 app 已装在机上（本机 Pixel 9 已装 `com.google.android.apps.maps`）。
- 多设备时用 `RELAY_ANDROID_SERIAL` / `ANDROID_SERIAL` 选设备。

## 凭证（SJTU 网关 qwen）

复用 RelayAgent 的 `.env`（**别提交、别复述完整 key**）：

| 参数 | 值 |
| --- | --- |
| `--llm_base_url` | `http://yjs-ipads.ipads-lab.se.sjtu.edu.cn:3000/v1` |
| `--model_name` | `qwen` |
| `--api_key` | `.env` 里的 `LLM_API_KEY` |
| `--agent-type` | `general_e2e`（qwen-3.5 适用，相对坐标 0–1000） |

## 步骤

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
  --llm-base-url http://yjs-ipads.ipads-lab.se.sjtu.edu.cn:3000/v1 \
  --max-round 25 \
  --timeout 600
```

把 key 喂进去而不落进 shell history 的写法：

```bash
export LLM_API_KEY=$(grep '^LLM_API_KEY=' .env | cut -d= -f2-)
uv run mw test "..." ... --api_key "$LLM_API_KEY"
```

## ⚠️ 关键：先手动拉起目标 app，别让 agent 从桌面找

从**桌面**起跑时，qwen 在 Pixel 9 上会反复 `scroll up` 想开应用抽屉，但这台机从桌面上滑会
拉出**通知栏**，agent 看到通知栏又想"上滑关掉"，就此死循环（实测白烧到 step 9 仍没进 app）。

**解法：跑 `mw test` 前先 `monkey` 把目标 app 拉到前台**，agent 从 app 内开始就稳了
（外滩那次预开 Maps 后 5 步搞定）：

```bash
uv run python scripts/run_mobileworld.py "Live navigate to the Bund by Google Map" \
  --app com.google.android.apps.maps
```

goal 仍写完整意图（"Live navigate to the Bund by Google Map"），agent 会在 app 内
搜索 + 起导航，不受预开影响。

## 实时看屏 / 录屏

- 看实时设备画面：`uv run mw device`。
- 录屏默认不开启；给脚本加 `--record` 后会自动分段 `adb screenrecord`，任务结束时结束当前分段并用
  `ffmpeg` 合并到 `recording.mp4`：

```bash
uv run python scripts/run_mobileworld.py "Live navigate to the Bund by Google Map" --record
```

默认输出到：

```bash
recordings/mobileworld_<timestamp>/recording.mp4
```

也可以指定目录：

```bash
uv run python scripts/run_mobileworld.py "Live navigate to the Bund by Google Map" \
  --record-dir recordings/mw_bund_manual
```

> `adb screenrecord` 单段上限 180s；脚本会循环分段录制。若本机没有 `ffmpeg`，会保留
> `chunk_*.mp4` 和 `concat.txt`，不做合并。

## 实测示例（2026-06-07 已跑通）

| 项 | 值 |
| --- | --- |
| goal | `Live navigate to the Bund by Google Map` |
| 驱动 | SJTU 网关 qwen，`general_e2e` |
| 起点 | 预开 Google Maps（见上「关键」） |
| 结果 | **5 步**：点搜索框 → 输入 "The Bund" → 选中外滩(Zhongshan Rd E-1, Waitan, Huangpu) → Start → 进入实时逐向导航（35 km / 42 min / 蓝色路线）|
| 录屏 | `recordings/mw_bund_<ts>/recording.mp4`（1080×2424，约 86s）|

对照：**不预开、从桌面起**那次，agent 卡在 `scroll up` 死循环烧到 step 9 没进 app —— 故必须预开。

## 模型 / 坐标系参考

`docs/real-devices.md`（MobileWorld 仓库内）的对照表：

| 模型 | agent-type | 坐标系 |
| --- | --- | --- |
| qwen-3.5 | `general_e2e` | 相对 0–1000 |
| Gemini 3 Pro | `general_e2e` | 相对 0–1000 |
| Claude Opus/Sonnet | `general_e2e` | 绝对像素（Sonnet 需 resize 到 1280×720） |
| Seed-2.0-Pro | `seed_agent` | 相对 0–1000 |

## 注意

- 这是真机直驱，会真实改设备状态（起导航、定位等）。导航类目标依赖设备真实 GPS / 网络；
  在国内需保证 Google 服务可达。
- 模拟器模式（`uv run mobile-world ...` 跑 Docker 快照）可能没有真实 GPS / 实时导航，
  录导航演示请用 real-device 模式。
- 命令里 **绝不** 硬编码 API key；用 `$LLM_API_KEY` 从 `.env` 注入。
