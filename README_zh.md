<h1 align="center">RelayAgent</h1>

<p align="center">
  <b>把子任务委派给 App 内置 AI 助手的移动端任务自动化</b>
</p>

<p align="center">
  <a href="#-引用">📚 论文</a> •
  <a href="android/README.md">📱 端上 App</a> •
  <a href="SPEC.md">📋 规范 v0.1</a> •
  <a href="docs/roadmap.zh.md">🗺️ 路线图</a> •
  <a href="CONTRIBUTING.md">🤝 参与贡献</a>
</p>

<p align="center">
  <a href="README.md">English</a> | <b>中文</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License">
  <img src="https://img.shields.io/badge/Python-3.12-blue.svg" alt="Python 3.12">
  <img src="https://img.shields.io/badge/SPEC-v0.1-green.svg" alt="SPEC v0.1">
  <img src="https://img.shields.io/badge/PRs-welcome-red.svg" alt="PRs welcome">
</p>

RelayAgent 是一个移动智能体：它把每个子任务**委派给 App 本就内置的 AI 助手**（复用用户已有的登录与上下文），只有在没有助手覆盖时才回落到 GUI 控制。

<p align="center">
  <img src="assets/paper/arch.png" alt="RelayAgent 架构：一条自然语言请求被分解为子任务，每个子任务委派给合适的 App 内置助手" width="820">
  <br>
  <em>一条请求、多个内置助手：planner 分解指令，把每个子任务委派给那个本就握着用户上下文的 App 内助手。<b>找一家附近餐厅</b> → 内容发现助手；<b>导航过去</b> → 地图助手。</em>
</p>

---

## 📖 关于

RelayAgent 把「把一个任务委派给一个 App 的助手」升级成一套完整的跨 App 智能体架构，由三块组成：

- **🧭 基于委派的 NL flow。** 把请求分解成 App 内子任务，为每步路由到一个 App，再把委派落成直 adb 的确定性动作（冷启动 → 入口路径 → 输入 prompt → scrape 回复 → 交还控制），结果经共享黑板传递——带执行期失败恢复与 GUI 兜底。*(`agents/flow/`；规范 §5)*
- **📚 动态能力库。** 每个 App 建模成一张**卡片（card）**——一份 schema 校验的 manifest，描述助手的入口路径、能力，以及 **handoff-to-user 安全策略**（不可逆动作必须先 `ask_user`）。能力边界从执行经验里学习，指导路由与兜底。*(`manifests/`；规范 §4)*
- **🧪 RelayBench 基准。** 30 个长时序日常任务，覆盖现有基准未覆盖的 App。*(`benchmark/`)*

### 第三种载体，逐项对比

|  | 厂商 API<br>(A2A / App Intents / AppFunctions) | 纯 GUI 智能体<br>(Mobile-Agent、AppAgent…) | **RelayAgent（本项目）** |
| --- | --- | --- | --- |
| 谁来做任务 | App 暴露的 API | 智能体重新驱动整套 UI | App **自己已登录**的内置助手 |
| 需要厂商配合 | **必须** | 不需要 | **可选** |
| 覆盖面 | 窄（只有已发布的接口） | 任意 App | 有助手的任意 App，**否则 GUI 兜底** |
| 用户上下文（登录/地址/支付） | 需重新提供 | 每次重新导航 | **本就在场** |
| 单个 App 内任务的成本/时延 | 低（一次调用） | **高**（每步截图 + VLM + 点击） | **低**（一句 NL 指令） |
| 不可逆动作安全 | API 层 | 临时凑 | **`handoff_to_user_required` 契约** |

2026 年厂商正把**自己的**助手接进**自己的**服务（阿里千问 App 已覆盖淘宝闪购、飞猪、高德打车）——这印证了前提，也把 RelayAgent 框定在**跨厂商 / 长尾**这块第一方整合永远触达不到的空隙。

<p align="center">
  <img src="assets/paper/gui-agent.png" alt="纯 GUI 智能体：每步一次截图 + 一次 VLM 往返" width="720">
  <br>
  <em><b>纯 GUI 智能体：</b>每步一次截图 + VLM 往返。</em>
  <br><br>
  <img src="assets/paper/dele-agent.png" alt="RelayAgent 委派：卡片给出确定性入口脚本，内置助手做任务" width="720">
  <br>
  <em><b>卡片委派：</b>一段确定性入口脚本（开 App → 输入 prompt → 提交）把任务交给内置助手。</em>
</p>

## 📊 评测

RelayAgent（**RA** = 带 GUI 兜底的 NL flow）在三个真机基准上全面胜过纯 GUI 基线（MobileWorld 的 `general_e2e`）——**AndroidDaily**、**MobileWorld** 与我们自建的 **RelayBench**：

- **📈 成功率高 6–15 个百分点** —— 46 / 49 / 83% vs. 31 / 34 / 77%
- **⚡ 端到端快 1.8×**（1.4–2× 区间）
- **🪙 LLM token 省 8.8×**（7–10× 区间）

<p align="center">
  <img src="assets/paper/fig1_completion_bars.png" alt="成功率：RA 46/49/83% vs 基线 GUI 智能体 31/34/77%（AndroidDaily / MobileWorld / RelayBench）" width="620">
</p>

同一台 Pixel 9、同一骨干模型；每个任务由两套系统在相同状态下执行、任务间冷启动复位，成败由人对轨迹 + 结果双判。速度/token 收益是在无需兜底完成的任务上；一旦需要 GUI 兜底，委派只多加 ~4 秒（7%）与 ~10 K token（5%）——因为能力边界建模够准，避免了无用的委派。

<details>
<summary><b>逐任务 token 消耗 &amp; 单任务 A/B（技术报告 §8）</b></summary>

<br>

<p align="center">
  <img src="assets/paper/fig3_paired_tokens.png" alt="逐任务 token 消耗，RA vs 基线，按任务配对" width="820">
  <br>
  <em>逐任务 token，按任务配对：蓝 = RelayAgent，橙 = GUI 基线。阴影区是走 GUI 兜底完成的任务。</em>
</p>

一组四配置的真机 A/B 隔离出**在单任务 T1（一次下单三杯蜜雪）上委派带来的收益**。四个配置都用同一个后端下同一单（淘宝闪购的助手*就是*千问），只变交互方式。中位 token，n=3：

| 配置 | 中位 token | vs RA 优化版 |
| --- | ---: | ---: |
| 纯 VLM，手动驱动原生 UI | 75 463 | 18.9× |
| 纯 VLM，*使用*内置助手（`general_e2e`） | 77 347 | 19.4× |
| RelayAgent，关掉优化（基线） | 9 585 | 2.4× |
| **RelayAgent，优化版** | **3 986** | **1×** |

- **结构化委派**赢过「用同一个助手」的纯 VLM 智能体，因为它去掉了每步的 VLM 重新驱动——具体是 **~1 张截图 vs ~30 张**（≈97–99% 是图像 prompt token）。无 manifest 的委派中继落在中间，说明差距**主要来自委派、manifest 次要**（§8.9）。
- **两个 App 无关的优化**（两段式回复完成 precheck + a11y-scrape 优先的取文路径）在未优化委派基线之上再叠 **2.4×**（§7）。
- **可预测性本身就是结果。** RA 单任务成本近乎恒定（T1 **3987 / 3986 / 3950** token，VLM 调用固定 2 次），而纯 VLM 智能体在同一任务上从 **38k → 97k token、46 → 379 秒**乱跳。折算成钱：RA ~**$0.001/任务** vs. 纯 VLM ~**$0.016**（16.6×）。
- **安全守住。** 每次 `handoff_to_user_required` 运行都在不可逆 CTA 前停住、零确认点击；**28/28 个能力**跨 7 张卡片都到达预期终态（§8.2.1）。

完整方法、有效性威胁、冻结数据：技术报告与基准数据（尚未发布，撰写中）。

</details>

## 🎬 演示

**单 App 下单** —— *「帮我点三杯蜜雪冰城蜜桃四季春，温度和糖度都用默认」* → 千问内置助手装配好 3 杯的购物车，然后**停在支付页**等用户确认（handoff 契约的实际效果）。

<p align="center">
  <img src="assets/RelayAgentDemoOrder/RelayAgentDemoOrder.gif" alt="经内置助手下单，支付前停住" width="320">
</p>

## 🗂️ 仓库结构

```
RelayAgent/
├── SPEC.md                    # manifest 规范 (v0.1) + 开放问题
├── spec/                      # schema.json（manifest）+ profile.schema.json（用户记忆）
├── manifests/                 # 每 App 一张 YAML 卡片；10 张安卓卡、50 个能力
├── agents/                    # 设备后端、LLM client、运行时循环、路由、中继适配器、NL flow
│   ├── device/                #   DeviceBackend 抽象（Android adb；iOS/HarmonyOS 接缝）
│   ├── llm/ · runtime/        #   provider client + 重试；进程入口 + 设备循环
│   ├── routing/ · agent/      #   capability/card 路由；端内 VLM agent + action 层
│   └── flow/                  #   NL 跨 App flow：planner、runner、leg judge、恢复
├── scripts/                   # run_plan.py（NL flow）、benchmark runner、校验、指标
├── android/                   # 端上 App：完整 NL flow 跑在手机上，无电脑、无 adb
├── benchmark/                 # A/B 基准任务集（含 RelayBench）
├── tests/                     # 无设备单测（CI）
├── docs/                      # 设计文档——见下「文档」
└── LICENSE                    # Apache-2.0
```

## 🚀 快速开始

```bash
git clone https://github.com/ShadowNearby/RelayAgent.git && cd RelayAgent
uv venv --python 3.12 && uv sync --no-install-project --extra dev
cp .env.example .env          # 然后填 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
uv run python -m agents.runtime.native_runner com.aliyun.tongyi "帮我点三杯蜜雪冰城蜜桃四季春"
```

这就是整个闭环：自己 load `.env`、激活 AdbKeyboard 输入法、冷启动目标 App、经直 adb 驱动目标。下面逐步解释：

### 1. 环境准备

RelayAgent 是纯 Python——**无 server、无框架冷启动**。需要 **Python 3.12** 的 Linux/WSL 主机，靠 `uv run` 直接跑源码（不安装本项目）。可选 extras：`--extra stream`（scrcpy 流式抓帧）、`--extra mw`（MobileWorld A/B 基线）。

### 2. 设备准备

USB 调试的安卓手机（或模拟器，见 [模拟器测试](docs/emulator_testing.zh.md)），装好 `com.android.adbkeyboard/.AdbIME`。完整设备准备与逐基准 App 需求见 [设备准备](docs/device_setup.zh.md)。

```bash
uv run python scripts/validate/check_device_env.py    # 跑前体检：设备/IME/uiautomator/screencap/App 安装态
```

### 3. 模型配置

把 `.env.example` 复制成 `.env`，填一个 OpenAI 兼容的 VLM 端点（`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`）。VLM **provider 无关**——Qwen-VL、Claude、Gemini、Kimi……——每任务用得很省（上面那些 token 数就是这么来的）。

### 4. 运行

上面 TL;DR 那条就是**单 App** 入口：`python -m agents.runtime.native_runner <pkg> "<goal>"` 冷启动一个目标包、进程内跑 `obs → predict → execute` 循环驱动一条目标。

要跑**自然语言跨 App flow**，用 `run_plan.py`——合成 flow、每个 leg 经矩阵路由、预览、再执行（执行期失败恢复 + 覆盖兜底 + 用户记忆 profile 默认开）：

```bash
uv run python scripts/run_plan.py --yes     "帮我点三杯蜜雪冰城蜜桃四季春"
uv run python scripts/run_plan.py --yes     "帮我找一台适合学生的平板电脑，预算2000以内"
uv run python scripts/run_plan.py --dry-run "把这段材料整理成一份中文总结文档"
```

适配器遵守 `handoff_to_user_required`：任何不可逆能力都在终端 CTA 前发 `ask_user`，而不是自动确认。

<details>
<summary><b>常用 env 旋钮</b>（完整清单见 <code>.env.example</code>）</summary>

<br>

| 旋钮 | 作用 |
| --- | --- |
| `RELAY_CAPTURE_BACKEND=scrcpy` | 流式抓帧（~1.5 秒 → ~8 ms/帧；需 `--extra stream` + 主机装 scrcpy）。任何失败永久回退 `screencap`。 |
| `RELAY_PRECHECK=0 RELAY_SCRAPE=0` | 关掉两个回复路径优化（复现基准 baseline）。 |
| `RELAY_RECOVERY=0` | 关掉执行期失败恢复。 |
| `--no-general-fallback` / `--no-mw-fallback` | 关掉 `run_plan.py` 的覆盖兜底。 |
| `RELAY_PROFILE=0` | 关掉用户记忆 profile 层。 |
| `RELAY_ANDROID_SERIAL=...` | 多设备时把每次 adb 调用钉到一台设备。 |
| `RELAY_MANIFESTS=/path` | 覆写默认 `./manifests/`。 |

</details>

### 跑测试

```bash
uv run python -m unittest discover -s tests -v            # 无设备；planner/runner 单测，不需 adb
uv run python scripts/validate/validate_manifests.py      # manifest schema + prompt_template 规则（CI gate）
```

真机 A/B 基准：`scripts/run_benchmark_test.py`。`test-results/` 与 `traj_logs/` 已 gitignore——别提交含用户数据的轨迹。

## 📱 端上 App

整条 pipeline——路由、规划、leg 执行、日志——也能跑在一个**独立 Android App** 里，靠无障碍服务 + Chaquopy 嵌入 Python：**无电脑、无 adb**。会话式任务流、实时运行卡、结构化运行日志查看器、中文界面 + 深色主题。主机行为逐字节保持，只有 Android 侧换实现。见 [`android/`](android/README.md)。

## 📚 文档

| 文档 | 说明 |
| --- | --- |
| [NL 跨 App flow](docs/nl_flow.zh.md) | 核心架构：合成、三段式路由、leg judge、失败恢复、覆盖兜底 |
| [Manifest 约定](docs/manifest_conventions.zh.md) | 写卡片：语言约定、`prompt_template`、`x_capture_full_reply`、能力关键字段 |
| [设备准备](docs/device_setup.zh.md) / [模拟器测试](docs/emulator_testing.zh.md) | 真机准备；AVD 搭建 + 远程观察 |
| [路线图](docs/roadmap.zh.md) | 产品化阶段 P1–P5 及验收指标（P1–P3 已落地） |
| [端上 App](android/README.md) | Android App：架构、主机↔设备接缝、界面 |

## 📇 支持的参考卡片

**10 个已验证安卓 App · 50 个声明能力**（高德、通义千问、携程、Gemini、小红书、微信、WPS、Reddit、Booking.com、Microsoft Copilot）。完整表格、能力与质量门槛见 **[docs/cards.zh.md](docs/cards.zh.md)**。提交卡片见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 🚧 本项目**不是**什么

- **不是 GUI 智能体。** 我们导航到内置助手的输入框，而非 App 的通用 UI。通用 GUI 智能体请看 Mobile-Agent / AppAgent / AutoGLM。
- **不是爬虫。** 卡片描述的是入口路径与能力，不是数据抽取。
- **不隶属任何手机厂商或 App 厂商。** 中立的社区规范——厂商可发官方卡片也可不发，社区两种情况下都能写。
- **不是要挑战 A2A 或 MCP。** 设计上前向兼容（规范 §14）。当 App 上了 A2A，卡片就变成更薄的 shim 或直接消失。

> **已知阻塞。** 淘宝托管的购物能力（现经千问卡片路由，因为淘宝内置助手*就是*千问）可能在 deep-link 目标页撞上服务端风控（「亲，访问被拒绝」）——这是账号/设备级风控墙，**不是**适配器或 manifest 的 bug。缓解：用有正常购买历史的账号、清掉待处理的实名 / 设备信任校验。

## 📚 引用

如果 RelayAgent 对你有帮助，请引用论文与本仓库：

```bibtex
@misc{relayagent2026,
  title        = {RelayAgent: Mobile Task Automation by Delegating Subtasks to In-App AI Assistants},
  author       = {Yan, Jingsheng and Wu, Fangnuo and Dong, Mingkai and Chen, Haibo},
  year         = {2026},
  howpublished = {\url{https://github.com/ShadowNearby/RelayAgent}},
  note         = {Preprint; arXiv release upcoming}
}
```

## 🙏 致谢

- [**MobileWorld**](https://github.com/Tongyi-MAI/MobileWorld)（Tongyi MAI）—— 我们三个评测基准之一，也是 A/B 对比里 `general_e2e` 纯 GUI 基线的来源。
- **AndroidDaily** —— 全系统评测里用到的日常任务基准（32 个中文 App）。

## 📄 许可

Apache-2.0，见 [LICENSE](LICENSE)。选宽松许可是为了企业友好——这套设计只有在手机厂商能无法律摩擦地采用时才成立。
