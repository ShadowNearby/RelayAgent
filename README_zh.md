# RelayAgent

<p align="center">
  <a href="report/RelayAgent-TechReport.md">技术报告</a> •
  <a href="SPEC.md">规范 (v0.1)</a> •
  <a href="docs/roadmap.zh.md">路线图</a> •
  <a href="CONTRIBUTING.md">参与贡献</a> •
  <a href="https://github.com/ShadowNearby/RelayAgent/issues">Issues</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License">
  <img src="https://img.shields.io/badge/Python-3.12-blue.svg" alt="Python 3.12">
  <img src="https://img.shields.io/badge/SPEC-v0.1-green.svg" alt="SPEC v0.1">
  <img src="https://img.shields.io/badge/PRs-welcome-red.svg" alt="PRs welcome">
</p>

**一个把用户请求「委派」给手机 App 内置 AI 助手的发现层（discovery layer）** —— 让操作系统级智能体（HarmonyOS 小艺、Apple Intelligence……）可以把任务交给那个**已经持有用户账号、地址、支付上下文**的 App 内置助手，而不必自己重新驱动整套 UI，也不必干等厂商发布接口。

每个 App 一张机器可读的**卡片（card）**。默认走 GUI 中介，厂商配合**可选不强制**。

<p align="center">
  <img src="assets/paper/arch.png" alt="RelayAgent 总览：一条自然语言请求被分解为子任务，每个子任务委派给合适的 App 内置助手" width="820">
  <br>
  <em>一条请求、多个内置助手：planner 分解指令，把每个子任务委派给那个本就握着用户上下文的 App 内助手。</em>
</p>

> **状态：** 早期，但已有实测。SPEC v0.1、10 张已验证的安卓参考卡片（50 个声明能力）、一个原生 Android 中继适配器、一套带执行期失败恢复与覆盖兜底的 NL 跨 App 规划器、一个端上 Android App，以及与纯 GUI 基线的真机对比基准。完整方法与数据见 [**技术报告**](report/RelayAgent-TechReport.md)。欢迎贡献。

## 📢 更新

- **2026-07-08 —— 路线图 P1–P3 落地。**（1）*执行期失败恢复*：失败的 app leg 先分类（`env_fail`/`route_fail`/`app_fail`），再爬梯子——重试（route_fail 先 LLM 换措辞）→ 换路由到别的 App → 运行期兜底 → 部分成功报告——benchmark 侧带逐次尝试的 token 遥测（[nl_flow §6.1](docs/nl_flow.zh.md)）。（2）*流式抓帧*：`RELAY_CAPTURE_BACKEND=scrcpy` 用常驻 H.264 流替换 ~1.5 秒的 `screencap`（Pixel 9 实测 **~8 ms/帧**），帧到达稳定检测替换各处固定 sleep。（3）*用户记忆*：磁盘 profile 注入规划与槽位抽取（「导航回家」直接解析成存的地址），偏好写入先问 y/n，轨迹落盘可脱敏。
- **2026-07-07 —— 端上 Android App 里程碑。** 完整 NL flow 现在跑在一个独立 App 里（[`android/`](android/README.md)，无障碍服务 + Chaquopy 嵌入 Python）——**无电脑、无 adb**：会话式任务流主页、实时运行卡、结构化运行日志查看器、中文界面 + 深色主题；仪器化测试套件在 Pixel 9 真机验证通过。同时发布[产品化路线图](docs/roadmap.zh.md)。
- **2026-06-14 —— 仓库重整 + CI。** `agents/` 按功能域拆成子包（`device/`、`llm/`、`runtime/`、`routing/`、`agent/`、`flow/`），上 GitHub CI，可选 extras（`dev`/`mw`/`stream`），manifest 与设备校验工具链。

## 📋 目录

- [第三条路](#第三条路)
- [工作原理——三块拼图](#工作原理三块拼图)
- [不止一张卡片：跨 App flow](#不止一张卡片跨-app-flow)
- [演示](#演示)
- [实测结果](#实测结果)
- [仓库结构](#仓库结构)
- [运行（真机、多 VLM）](#运行真机多-vlm)
- [运行测试](#运行测试)
- [文档](#文档)
- [MVP 范围（v0.1）](#mvp-范围v01)
- [已知阻塞点](#已知阻塞点)
- [这个项目不是什么](#这个项目不是什么)
- [参与进来](#参与进来)

## 第三条路

操作系统级智能体想在第三方超级 App **内部执行操作**，过去只有两条路，且在开放生态里都会失效：

- **厂商配合型 API** —— A2A、Apple App Intents / Shortcuts、Android AppFunctions、HarmonyOS HMAF。干净、强类型，但前提是**厂商**得发布一个接口。长尾 App——以及大多数国产超级 App——根本不发。
- **纯 GUI 智能体** —— Mobile-Agent、MobiAgent、AppAgent、AutoGLM…… 从截图出发逐步驱动整套 UI。脆弱、慢、易被检测、法律灰色，而且**浪费**：它们要把登录、定位、支付状态这些 App 本就持有的东西重新跑一遍。

RelayAgent 背后的观察是：大多数超级 App **本身就已经内置了一个登录态的 AI 助手** —— 高德的助手 tab、微信里的元宝、淘宝/闪购的购物助手、小红书的点点、WPS AI。对相当大一部分真实意图来说，用户想要的能力*本就存在、本就登录、本就握着用户的上下文*，就在 App 里面。

所以我们主张**第三条路：把用户意图委派给 App 自己的内置助手** —— 而不是自己重做任务（纯 GUI 智能体），也不是等接口（A2A）。这条路需要的不是更聪明的自动化模型，而是一份*契约*：一个按 App 组织的发现层，告诉 OS 智能体哪些 App 内置了助手、它的输入框在哪、它能做什么，以及——最关键的——**什么时候必须把控制权交还给用户。**

同一个导航任务，两条路对照：

<p align="center">
  <img src="assets/paper/gui-agent.png" alt="纯 GUI 智能体：每一步一张截图 + 一次 VLM 往返" width="820">
  <br>
  <em><b>纯 GUI 智能体</b> —— 每一步都是截图 + VLM 往返：找搜索框、输入、选结果、点开始……一个任务几十次视觉调用。</em>
</p>

<p align="center">
  <img src="assets/paper/dele-agent.png" alt="RelayAgent 委派：卡片提供确定性入口脚本，内置助手执行任务" width="820">
  <br>
  <em><b>经卡片委派</b> —— manifest 提供确定性入口脚本（打开 App → 输入模板化 prompt → 提交）；真正干活的是握着用户上下文的内置助手。</em>
</p>

| | 厂商 API<br>(A2A / App Intents / HMAF) | 纯 GUI 智能体<br>(Mobile-Agent、MobiAgent…) | **RelayAgent（本项目）** |
| --- | --- | --- | --- |
| 谁来执行任务 | App 暴露的 API | OS 智能体重新驱动整套 UI | App **自己登录态**的内置助手 |
| 厂商配合 | **必须** | 不需要 | **可选** |
| 用户上下文（登录、地址、支付） | 需重新提供 | 每次重新走一遍 | **本就存在** |
| 不可逆操作的安全保障 | API 层 | 临时拼凑 | **`handoff_to_user_required` 契约** |
| 成本可预测性 | 高 | **低**（run 间方差大） | **高**（几乎完全一致） |

2026 年还在快速冒头一个「邻居」：厂商把*自家*助手直接接进*自家*服务，让用户在一个超级助手里完成全部交易（阿里的 Qwen App 已吸收淘宝闪购、飞猪、高德打车）。这件事一方面**验证了前提**——内置助手确实能在登录态下完成真实交易——另一方面也**框定了我们的定位**：这类整合是第一方、生态内的。RelayAgent 瞄准的是任何超级助手都覆盖不到的**跨厂商 / 长尾缺口**。哪里已经有第一方整合，RelayAgent 就退让给它。（完整定位见技术报告 §2。）

## 工作原理——三块拼图

1. **发现（Discovery）—— 卡片。** 每个 App 一份 YAML manifest（`manifests/*.yaml`，由 `spec/schema.json` 做 JSON-Schema 校验），描述进入内置助手的 launcher 入口路径、能力列表、示例 prompt、延迟提示和 handoff 策略。*（规范见 §4。）*
2. **接入（Access）—— 中继适配器。** `agents/agent/relay_agent.py` 直 adb 把一张卡片落地为确定性的设备动作：冷启动 → 走入口路径 → 输入 prompt → 等待回复 → 抓取文本 → handoff。优先走无障碍树（accessibility tree），且对各家 VLM 通用。*（§5。）*
3. **安全（Safety）—— handoff 契约。** 标了 `handoff_to_user_required: true` 的能力*必须*在**任何不可逆操作之前**——支付、确认叫车、提交订单——发出 `ask_user` 并交还控制权。可逆的准备工作由内置助手完成；不可逆的那一步由人来授权。*（§4.1。）*

### 一张卡片是怎么用的

```
用户：「叫一辆经济型车去机场」
        │
        ▼
OS 级智能体
  1. 收到一个明确的目标 App，例如 com.autonavi.minimap
  2. 把请求和卡片里的能力做匹配 → 选中 `hail_ride`
  3. 按 card.entry 操作 → 打开高德、点 AI tab、切到文字模式
  4. 把用户的原始 prompt 输入到聊天框
  5. 遵守 `handoff_to_user_required: true` —— 在「立即打车」CTA 之前交还控制权
        │
        ▼
高德的内置助手干真正的活（它本就认识这个用户）
```

目标 App 是**显式指定**的。OS 智能体在其中选一个能力。内置助手执行。卡片就是那份契约。

## 不止一张卡片：跨 App flow

一张卡片回答的是「怎么把*这个*任务交给*这个* App」。v0.1 之后落地的所有东西，把这个原语扩展成了一套完整的智能体架构——分解、委派、编排：

- **NL 规划 + 三段式路由。** `scripts/run_plan.py` 从一条自然语言请求合成多 App flow；每一步经三段式路由（非 foundation 预筛 → 重排 → foundation 兜底独立成段）对着[能力矩阵](docs/app_capability_matrix.csv)解析 app + capability，blackboard 在 leg 间传递结果。（[NL flow 架构](docs/nl_flow.zh.md)）
- **执行期失败恢复**（路线图 P1，默认开）。失败的 leg 先分类，再爬梯子：重试（route_fail 先 LLM 换措辞）→ 换路由到别的 App → 运行期兜底 → 部分成功报告（`flow_report.json`）。带 handoff 契约的能力只允许重试——绝不绕过安全契约换路由。（[nl_flow §6.1](docs/nl_flow.zh.md)）
- **覆盖兜底。** 没有任何卡片覆盖某条 leg——或恢复梯子爬完——时，这条 leg 交给同一 runtime 上的无 manifest 通用 GUI agent（装了 `mw` extra 时则交给 MobileWorld 的 `general_e2e`），而不是直接放弃。优先级：MW > general > unsatisfiable。（[nl_flow §10](docs/nl_flow.zh.md)）
- **流式抓帧 + 稳定检测**（P2）。`RELAY_CAPTURE_BACKEND=scrcpy` 把每步 ~1.5 秒的 `screencap` 变成 ~8 ms 读一帧最新解码缓冲；「quiet 窗口内无新帧」替换固定 sleep。任何失败永久回退 `screencap`，不搞重启风暴。
- **用户记忆**（P3，默认开；无 profile 文件即 no-op）。YAML profile（`spec/profile.schema.json`）注入规划与模板槽位抽取；偏好只在显式 y/n 确认后写入；`RELAY_TRAJ_REDACT=1` 在所有日志落盘点把 profile 值换成占位符。
- **路由固化。** 有 verdict 背书的路由固化成 0-LLM 查表，重复请求不再花 token。（[nl_flow §9](docs/nl_flow.zh.md)）
- **端上 App。** 整条 pipeline——路由、规划、leg 执行、日志——跑在一个独立 Android App 里（无障碍服务 + 嵌入式 Python）：无电脑、无 adb。（[`android/`](android/README.md)）
- **平台接缝。** 设备 I/O 走后端抽象层（`agents/device/`；Android=直 adb，iOS/HarmonyOS 骨架），manifest 声明 `platforms` / 各平台 `app_ids`。（[设备后端](docs/device_backends.zh.md)）

## 演示

一段真机端到端运行：

**T1 —— 单 App 下单。** *「帮我点三杯蜜雪冰城蜜桃四季春，温度和糖度都用默认」* → 千问（Qwen）内置助手凑好 3 杯的购物车，停在支付页等用户确认。

![通过内置助手下单](assets/RelayAgentDemoOrder/RelayAgentDemoOrder.gif)

## 实测结果

### 委派在单个任务上省下了什么（技术报告）

真机上的四配置 A/B（技术报告 §8）用来分离*委派到底省下了什么*。这里保留 **T1**：单 App 点三杯蜜雪冰城。所有配置下的**同一笔单子都走同一个后端**（淘宝闪购的助手*就是*千问），只有交互方式不同。中位数 token，RelayAgent / 纯 VLM 配置取 n=3：

**T1 —— order_food**

| 配置 | token 中位数 | 相对 RA optimized |
| --- | ---: | ---: |
| 纯 VLM，手动驱动原生 UI | 75 463 | 18.9× |
| 纯 VLM，*使用*内置助手（`general_e2e`） | 77 347 | 19.4× |
| RelayAgent，关闭优化（baseline） | 9 585 | 2.4× |
| **RelayAgent，开启优化** | **3 986** | **1×** |

逐层看这些梯度：

- **结构化委派（RelayAgent）**相对*同一个*内置助手的纯 VLM 驱动赢在去掉每一步的 VLM 重新驱动。这个差距具体就是 **~1 张截图 vs ~30 张**（成本里 ~97–99% 是图像 prompt token）。一个去掉 manifest 的委派中继落在两者中间，说明这个差距**主要来自委派，manifest 是次要项**（§8.9）。
- **两项与 App 无关的优化**（两阶段「回复是否完成」预检 + 优先走无障碍抓取的文本路径）在未优化的委派 baseline 之上再带来 **2.4×**（§7）。

**「可预测性」本身就是一个结果。** RelayAgent 每个任务的成本几乎恒定——T1 为 **3987 / 3986 / 3950** token（VLM 调用固定为 2 次）——而纯 VLM 智能体在同一个任务上波动于 **38k → 97k token、46 → 379 秒**（三次都到了同一个支付前页面），早期探索中还出现过过早退出和失控空转的长尾。对按 token 付费的人来说，一个可预测的 ~4k 胜过几倍的方差。换算成钱（§8.2），RelayAgent optimized 约 **$0.001/任务**，纯 VLM 约 **$0.016**（16.6×）。

**安全保障守住了。** 每一次 `handoff_to_user_required` 运行都停在了不可逆 CTA 之前——下单停在 `立即支付`——零次确认点击。冻结的 benchmark 目录中，7 张卡片的 **28/28 个能力**都到达了预期终态（§8.2.1）；当前 manifest 目录已扩展到 10 张卡片，Reddit Ask、Booking.com AI 聊天与 Microsoft Copilot 已单独真机验证。

> 上述数字为 2026-06-02 的 n=3 复跑结果；完整方法、有效性威胁与冻结数据见[技术报告](report/RelayAgent-TechReport.md)及 `report/benchmark-data-n3.md`。

### 三个基准上委派 vs GUI 智能体（论文评估）

整机评估（论文 preprint，arXiv 即将发布）把 RelayAgent（**RA** = 带 GUI 兜底的 NL flow）与纯 GUI 基线在三个真机基准上正面对比——**AndroidDaily**、**MobileWorld**、**DeleBench**（我们自建的 30 条长链路日常任务）——每条任务由两套系统在相同设备状态下先后执行，任务间冷启动复位：

<p align="center">
  <img src="assets/paper/fig1_completion_bars.png" alt="成功率：RA 46/49/83%，GUI 基线 31/34/77%（AndroidDaily / MobileWorld / DeleBench）" width="640">
</p>

- **三个基准上成功率全面更高** —— AndroidDaily / MobileWorld / DeleBench 上分别 46% / 49% / 83%，GUI 基线为 31% / 34% / 77%。内置助手覆盖到的任务由助手可靠执行，替掉脆弱的 GUI 动作序列；覆盖不到的由 GUI 兜底保底。
- **端到端快 1.4–2×**（平均 1.8×，取双方都无兜底成功的任务）；需要兜底时，委派尝试平均只多花 ~4 秒（总时长的 7%）——因为能力边界建模得足够准，不做无谓的委派尝试。
- **LLM token 少 7–10×**（平均 8.8×，无兜底时）；走兜底时平均只多 ~10K token（5%）：

<p align="center">
  <img src="assets/paper/fig3_paired_tokens.png" alt="逐任务 token 消耗，RA vs 基线，按任务配对" width="820">
  <br>
  <em>逐任务 token 配对图：蓝=RelayAgent（RA），橙=GUI 基线。阴影区是经 GUI 兜底完成的任务——助手覆盖到的地方委派大赢，覆盖不到的地方也几乎不多花钱。</em>
</p>

## 仓库结构

```
RelayAgent/
├── SPEC.md                    # manifest 规范 (v0.1)
├── SPEC-OPEN-QUESTIONS.md     # 仍在讨论的设计问题
├── spec/                      # schema.json（manifest）+ profile.schema.json（用户记忆）
├── manifests/                 # 每个 App 一张 YAML 卡片；10 张安卓卡片
├── agents/                    # 设备后端、LLM client、运行时循环、路由、中继适配器、NL flow
├── scripts/                   # run_plan.py（NL flow）、benchmark runner、校验、metrics
├── android/                   # 端上 App：完整 NL flow 跑在手机里，无电脑无 adb
├── benchmark/                 # A/B 基准的任务集
├── tests/                     # 无设备单元测试（CI）
├── docs/                      # 设计文档——见下方「文档」
├── report/                    # 技术报告 + 冻结的基准数据
├── CONTRIBUTING.md
└── LICENSE                    # Apache-2.0
```

## 运行（真机、多 VLM）

`agents/agent/relay_agent.py` 在进程内 `obs → predict → execute` 循环里直 adb 驱动 RelayAgent（无 server）。VLM 对各家通用（Claude、Gemini、Qwen-VL、Kimi……）；卡片负责提供确定性的入口路径和 handoff 策略。

需要 **Python 3.12**，主机为 Linux/WSL 装好 adb，外加一台开启 USB 调试的手机（或模拟器——见[模拟器测试](docs/emulator_testing.zh.md)），设备上装好 `com.android.adbkeyboard/.AdbIME`。完整设备准备 + 各 benchmark 的 App 需求见[端侧环境](docs/device_setup.zh.md)；跑前体检：`uv run python scripts/validate/check_device_env.py`。

```bash
# 1. 建 venv（靠 `uv run` 跑源码，不安装本项目）
uv venv --python 3.12
uv sync --no-install-project --extra dev
#   可选 extras：--extra stream（scrcpy 流式抓帧）、--extra mw（MobileWorld A/B 基线）

# 2. 填好 .env（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL），然后跑一个目标
uv run python -m agents.runtime.native_runner com.aliyun.tongyi "帮我点三杯蜜雪冰城蜜桃四季春"
```

`agents.runtime.native_runner` 会 load `.env`、激活 AdbKeyboard 输入法、经 `agents/device/` 后端抽象层冷启动目标 App（force-stop + monkey LAUNCHER）、设置 `RELAY_SKIP_OPEN_APP=1` 让 planner 跳过自己的 `open_app` 步、在进程内直 adb 跑循环，并把额外 flag（如 `--max-step 40`）原样转发给 agent。不走 `.env` 时可用 `--model` / `--base-url` / `--api-key` 覆盖 LLM 配置。

`--model` 对各家通用——指向任意 OpenAI 兼容的 VLM 即可（`qwen/qwen3-vl-235b-a22b`、`anthropic/claude-sonnet-4-5`、`google/gemini-3`……）。每个任务里 VLM 用得很省（这正是 §8 成本数字的来源）：

- 1 次纯文本 LLM 调用，从卡片里挑出能力。
- 每个文本选择器先试 `uiautomator dump`（精确、零 token）；只有 miss 时才发一次小的 VLM grounding 调用。
- `wait_for_reply` **确定性**判定回复完成——无障碍树文本哈希连续多拍 byte-identical 才算 done——预算按墙钟（`max(5×typical_latency, 60)` 秒）。一个两阶段预检（截图感知哈希 → 无障碍树文本哈希）会在回复还在流式输出时跳过昂贵的 dump。**不用 VLM 判 done**。
- 回复文本**从无障碍 dump 抓取**；只有 scrape 落空（WebView/canvas 回复）时才让 VLM 读帧。对 `x_capture_full_reply` 能力，每一帧滚动抓取也是 scrape。
- 卡片里的 `screen_fraction` 坐标只在无障碍树暴露不出元素时作为最后兜底。

可选环境变量（完整列表见 `.env.example`）：

- `RELAY_MANIFESTS=/path/to/manifests` —— 覆盖默认的 `./manifests/`。
- `RELAY_CAPTURE_BACKEND=scrcpy` —— 流式抓帧（~1.5 秒 → ~8 ms/帧；需 `--extra stream` + 主机装 scrcpy），并启用帧到达稳定检测；任何失败永久回退 `screencap`。
- `RELAY_PRECHECK=0 RELAY_SCRAPE=0` —— 关闭 §7 两项优化（复现基准 baseline）。
- `RELAY_TIMING=1` —— 写出每次运行的 `wall_clock.json`。
- `RELAY_FRESH_CONV=0` —— 跨 run 保留上一轮对话（默认每次开新对话）。
- `RELAY_ANDROID_SERIAL=...` —— 多设备时把每个 adb 调用钉到某一台。

适配器遵守 `handoff_to_user_required`：对任何不可逆能力，会在终态 CTA 之前发 `ask_user`，而不是自动确认。

### 自然语言入口

推荐用 `scripts/run_plan.py` 作为自然语言入口。它会合成 flow，对每个 app step 用共享的 matrix 三段式路由解析 app + capability，预览后执行（加 `--yes` 跳过确认）——执行期失败恢复与覆盖兜底默认开启（`RELAY_RECOVERY=0` / `--no-general-fallback` 关闭，见 [nl_flow §6.1 / §10](docs/nl_flow.zh.md)），存在 profile 时用户记忆自动注入（`RELAY_PROFILE=0` 关闭）。

```bash
uv run python scripts/run_plan.py --yes "帮我点三杯蜜雪冰城蜜桃四季春"
uv run python scripts/run_plan.py --yes "帮我找一台适合学生的平板电脑，预算2000以内"
uv run python scripts/run_plan.py --dry-run "把这段材料整理成一份中文总结文档"
```

## 运行测试

```bash
uv sync --no-install-project --extra dev
uv run python -m unittest discover -s tests -v            # 无设备；planner/runner 单元测试，不需要 adb
uv run python scripts/validate/validate_manifests.py      # manifest schema + prompt_template 规则（CI gate）
```

真机运行（不是单元测试）直接走入口脚本——见[运行](#运行真机多-vlm)：单 App 用 `python -m agents.runtime.native_runner <pkg> "<goal>"`，NL flow 用 `scripts/run_plan.py --yes`，A/B 基准用 `scripts/run_benchmark_test.py`。需要连好的安卓设备、装好目标 App，`com.android.adbkeyboard/.AdbIME` 可用（runner 自己启用/复位输入法）。

把 `.env.example` 复制成 `.env` 并填好（LLM endpoint 必填）。`test-results/` 和 `traj_logs/` 已 gitignore —— 不要提交含用户数据的轨迹。

## 文档

| 文档 | 内容 |
| --- | --- |
| [NL 跨 App Flow](docs/nl_flow.zh.md) | 合成、三段式路由、校验、leg judge、失败恢复、覆盖兜底、路由固化 |
| [跨 App 规划器](docs/cross_app_planner.zh.md) | planner pipeline、CLI 用法、plan 缓存、真机示例 |
| [Manifest 约定](docs/manifest_conventions.zh.md) | 语言约定、`prompt_template`、`x_capture_full_reply`、卡片 `swipe` 方向、capability 关键字段 |
| [Prompt 模板](docs/prompt_template.zh.md) | 模板化 submit prompt：槽位、可选段、加载期校验 |
| [能力分类法](docs/capability_taxonomy.zh.md) | capability id 背后的受控词表 |
| [设备后端](docs/device_backends.zh.md) | 多平台后端抽象层（Android adb、iOS/HarmonyOS 接缝） |
| [轨迹日志](docs/trajectory_logging.zh.md) | 日志目录形态、写入方、轮转、消费方 |
| [端侧环境](docs/device_setup.zh.md) | 真机准备 + 各 benchmark 的 App 需求 |
| [模拟器测试](docs/emulator_testing.zh.md) | AVD 搭建、安装步骤、远程观察 |
| [路线图](docs/roadmap.zh.md) | 产品化五阶段 P1–P5 与验收指标（P1–P3 已落地） |
| [端上 App](android/README.md) | Android App：架构、主机↔端侧接缝、界面 |

## MVP 范围（v0.1）

当前目录有 10 张已验证的安卓参考卡片，共 **50 个声明能力**：

| App | 包名 | 能力 | 卡片类型 |
| --- | --- | --- | --- |
| 高德地图 (Amap) | com.autonavi.minimap | POI 搜索、导航、打车、行程规划 | mixed |
| 通义千问 (Tongyi Qwen) | com.aliyun.tongyi | foundation_llm、火车/打车/外卖/酒店/电影活动预订、商品搜索/购买引导/订单追踪 | mixed |
| 携程旅行 (Ctrip) | ctrip.android.view | 机票、酒店、火车、景点、跟团游 | mixed |
| Gemini | com.google.android.apps.bard | foundation_llm、公共 Web 检索、授权后的 Google 服务读写任务 | mixed |
| 小红书 (Xiaohongshu) | com.xingin.xhs | 通过 AI 搜索做社区 UGC 问答 | multi-node |
| 微信 (WeChat) | com.tencent.mm | 元宝聊天界面、AI 搜索 | mixed |
| WPS Office | cn.wps.moffice_eng | AI 文档 / PPT / 写作辅助 | single-bubble |
| Reddit | com.reddit.frontpage | Reddit Ask 垂类社区检索与总结 | multi-node |
| Booking.com | com.booking | 旅行信息探索、行程规划、住宿搜索 | mixed |
| Microsoft Copilot | com.microsoft.copilot | foundation_llm、附近 POI 搜索、商品搜索 | single-bubble |

*卡片类型*（单气泡 TextView vs 多节点 RecyclerView）决定回复抽取策略——见技术报告 §4 / §5.4。

每张卡片的质量门槛：所有必填 SPEC 字段齐全、每个能力 ≥2 条真实示例 prompt、提交前 30 天内人工验证过、每个不可逆能力的 `handoff_to_user_required` 标注正确。

## 已知阻塞点

- **淘宝服务端风控（「访问被拒绝」）。** 淘宝购物能力现已并入 千问 (Qwen) 卡片、经 Taobao 后端路由——独立的 `com.taobao.taobao` 卡片已下线，因为淘宝内置助手本身*就是*千问，而走千问承载的路径会经过同一个履约后端、但不直接驱动淘宝 App 的 GUI（更安全的路径）。风控墙仍可能出现在 `purchase_guidance` / `order_food`（即淘宝闪购的本地配送卡片）的 deep-link 目标页：一个服务端渲染的「亲，访问被拒绝」墙（或一次性的实名/身份验证关卡），而不是商品 / 本地配送路径。这是账号级、设备级的风控，**不是**适配器或 manifest 的 bug：入口路径执行正确，失败发生在内置助手触发*之后*的 deep-link 目标页。缓解办法：用有正常购买记录的账号登录设备、在 我的淘宝 → 设置 → 账号与安全 里清掉待办的实名/设备信任校验、不要在刚刷机的设备上连续背靠背地跑同一个被风控的能力。

## 这个项目*不是*什么

- **不是 GUI 智能体。** 我们导航到内置助手的输入框，不驱动 App 的一般 UI。需要通用 GUI 智能体的话，见 MobiAgent / Mobile-Agent / AutoGLM。
- **不是爬虫。** 卡片描述的是入口路径和能力，不是数据抽取。一个合规的路由器不会去读用户没有放进去的 App 数据。
- **不隶属于任何手机 OEM 或 App 厂商。** 中立的社区规范。厂商可以发布官方卡片，也可以不发；社区无论如何都能自己写一张。
- **不是 A2A 或 MCP 的挑战者。** 设计上向前兼容（见 SPEC §14）。当 App 上了 A2A，卡片会退化成更薄的 shim，乃至消失。

## 参与进来

- **读设计：** 先看[技术报告](report/RelayAgent-TechReport.md)，再看 [SPEC.md](SPEC.md) 和 [SPEC-OPEN-QUESTIONS.md](SPEC-OPEN-QUESTIONS.md)。
- **接下来往哪走：** [产品化路线图](docs/roadmap.zh.md)——五个阶段，每阶段带验收指标。**P1（失败恢复）、P2（流式抓帧）、P3（用户记忆）已落地**；P4（卡片 CI 与半自动生成）、P5（多平台 / OEM 集成）开放认领。
- **提交卡片：** 见 [CONTRIBUTING.md](CONTRIBUTING.md)。
- **行为准则：** 见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
- **讨论：** GitHub Issues。

## 致谢

- [**MobiAgent**](https://github.com/IPADS-SAI/MobiAgent)（SJTU IPADS）—— 我们对标定位的纯 GUI 移动智能体路线，也是我们[技术报告](report/RelayAgent-TechReport.md)的结构范本。
- [**MobileWorld**](https://github.com/Tongyi-MAI/MobileWorld)（Tongyi MAI）—— 我们三个评估基准之一，也是 A/B 对比中 `general_e2e` 纯 GUI 基线的来源。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。选宽松许可是为了便于企业采用——这套设计只有在手机 OEM 能无法律摩擦地采纳时才成立。
