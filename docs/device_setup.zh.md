# 端侧测试环境（Device Setup）

> English: [`device_setup.md`](device_setup.md)

> 跑 RelayAgent（单 App 调试 / NL flow / A/B benchmark）之前，设备侧需要准备什么。
> 一键体检：`uv run python scripts/validate/check_device_env.py [--benchmark relaybench|androiddaily|mobileworld|all]`（无 FAIL 退出码为 0）。
> 模拟器（无真机）路径见 [`emulator_testing.zh.md`](emulator_testing.zh.md)。

## 1. 主机侧

| 项 | 要求 |
| --- | --- |
| adb | Android platform-tools，`adb` 在 PATH 上 |
| Python | 3.12（`uv venv --python 3.12 && uv sync --no-install-project`）|
| LLM 端点 | `.env` 填好 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` |
| MobileWorld（仅 A/B baseline / MW 兜底需要）| `third_party/MobileWorld` 符号链接到 sibling checkout（机器本地，见 [`mobileworld_real_device.md`](mobileworld_real_device.md)），否则用 pyproject 钉死的 git 快照 |

## 2. 设备通用要求（真机或模拟器都一样）

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 连接 | USB 调试或 Wi-Fi adb（`adb tcpip 5555` + `adb connect`）| Phase B 实测用 Wi-Fi adb；**涉及飞行模式的任务会杀掉 Wi-Fi adb 传输，已从任务集剔除** |
| 多设备 | `RELAY_ANDROID_SERIAL=<serial>` | 所有 adb 调用都遵守（`agents/runtime/_adb.py`）|
| 输入法 | **ADBKeyBoard**（`com.android.adbkeyboard`，[senzhk/ADBKeyBoard](https://github.com/senzhk/ADBKeyBoard)）已安装 | runner 启动时自己 `ime enable/set`，退出 `ime reset` 复位；只要求"已安装" |
| a11y dump | `uiautomator dump` 可用 | tap_text 定位与回复 scrape 的主路径；不可用时全部回落 VLM（慢、贵）|
| 截图 | `adb exec-out screencap -p` 可用 | 真机实测 ~1.5s/帧，是单步最大成本 |
| 亮屏 | `settings put global stay_on_while_plugged_in 7` | 防任务中途锁屏 |
| 语言/网络 | 中文垂类 App 建议系统中文 + 国内网络；Gemini / Copilot / Reddit / Booking 需要可达国际网络 + Google 服务（GMS）| 双栈网络环境最省事 |

## 3. App 需求（按用途分层）

### 3.1 核心：10 个 manifest App（covered 层全部收益所在）

所有 benchmark 的 covered 层都路由到这 10 个 App 的内嵌 agent，**必须装好并登录**：

| App | 包名 | 账号/前置 |
| --- | --- | --- |
| 通义千问 | com.aliyun.tongyi | 阿里账号登录（购物/外卖能力走淘宝后端，账号需正常购买历史，见 README「已知阻塞点」风控）|
| 高德地图 | com.autonavi.minimap | 登录 + 定位权限（打车需实名/支付绑定）|
| 携程旅行 | ctrip.android.view | 登录 |
| 微信 | com.tencent.mm | 登录（元宝 / AI 搜索入口）|
| 小红书 | com.xingin.xhs | 登录（点点 AI 搜索）|
| WPS Office | cn.wps.moffice_eng | 登录（AI 文档/PPT）|
| Gemini | com.google.android.apps.bard | Google 账号 + GMS + 国际网络 |
| Microsoft Copilot | com.microsoft.copilot | 微软账号 + 国际网络 |
| Reddit | com.reddit.frontpage | 账号 + 国际网络 |
| Booking.com | com.booking | 账号 + 国际网络 |

> 权限弹窗不用手工预清：`relay_agent` 的 `_maybe_dismiss_permission_popup` 会对白名单包自动点「允许」（每 task 上限 8 次，`RELAY_DISMISS_PERMISSIONS=0` 关）。

### 3.2 RelayBench（30 条）

只需 §3.1 的 10 个 App（套件就是围绕它们设计的，出现均衡，单任务 4–5 次/App）。

### 3.3 AndroidDaily（235 条；Phase B 只跑 71 条 covered）

- **covered 子集（71 条）**：任务指令虽点名 淘宝/饿了么 等，但 RA 把它们路由进 manifest App（如淘宝购物 → 千问，同一下单后端）→ 仍只需 §3.1。
- **MW 兜底层（143 条，若在真机全量跑）**：需要任务点名的原生 App。出现频次 Top：淘宝(15)、携程(14)、微信(14)、美团(12)、高德(11)、铁路12306(10)、小红书(9)、微博(9)、去哪儿(9)、飞猪(8)、京东(8)、拼多多(7)、哔哩哔哩、抖音、滴滴出行、饿了么、大众点评、网易云音乐、QQ音乐、知乎 …（全集 70+ App，见 `benchmark/androiddaily_task_info.csv` 的「APP名称」列）。全部需要登录态。

### 3.4 MobileWorld（201 → `--skip-mcp` 后 161 条）

MW 的任务跑在 **MobileWorld 自带的应用环境**（Mail、Messages、Mastodon、Files、Calendar、Mattermost、Chrome、Contacts、Gallery、Maps、Docreader、Clock、Settings、Camera、Taodian），由 MW 的任务初始化逻辑预置数据——**不是装国内 App**，环境搭建见 [`mobileworld_real_device.md`](mobileworld_real_device.md)。注意：

- `MCP-*` 任务（40 条）是 tool-call 非真 GUI，`--skip-mcp` 剔除。
- Taodian（淘typed电商示例 App）在真机上两系统都失败（风控），Phase B 记 both-fail。

## 4. 跑 benchmark 前的状态卫生

- **每个 App open 前必 cold-launch**（`am force-stop` + monkey LAUNCHER，runner 自动做）——不要手工预开 App 留热状态。
- 任务间 `kill_all_apps()` 硬复位（force-stop 所有在跑的三方包 + HOME），防上一任务的聊天线程/半成单泄漏进下一任务。
- 评测公平性开关（`run_benchmark_test.py` 默认强制）：`RELAY_ROUTE_OVERLAY=0`、`RELAY_STEP_LOG=0`、`RELAY_CAPTURE_FULL_REPLY=0`、relay 走 `--no-cache`。详见 [`evaluation.zh.md`](evaluation.zh.md) §8。

## 5. 速查

```bash
# 体检（核心检查 + 10 个 manifest App 是否安装）
uv run python scripts/validate/check_device_env.py

# 指定 benchmark / 指定 App 集 / 指定设备
uv run python scripts/validate/check_device_env.py --benchmark mobileworld
uv run python scripts/validate/check_device_env.py --apps com.aliyun.tongyi,com.autonavi.minimap
uv run python scripts/validate/check_device_env.py --serial 46180DLAQ004LW
```
