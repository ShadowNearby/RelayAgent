# RelayAgent

<p align="center">
  <a href="report/RelayAgent-TechReport.md">技术报告</a> •
  <a href="SPEC.md">规范 (v0.1)</a> •
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

> **状态：** 早期，但已有实测。SPEC v0.1、10 张已验证的安卓参考卡片（50 个声明能力）、一个原生 Android 中继适配器，以及一组真机 A/B 基准测试。完整方法与数据见 [**技术报告**](report/RelayAgent-TechReport.md)。欢迎贡献。

---

## 第三条路

操作系统级智能体想在第三方超级 App **内部执行操作**，过去只有两条路，且在开放生态里都会失效：

- **厂商配合型 API** —— A2A、Apple App Intents / Shortcuts、Android AppFunctions、HarmonyOS HMAF。干净、强类型，但前提是**厂商**得发布一个接口。长尾 App——以及大多数国产超级 App——根本不发。
- **纯 GUI 智能体** —— Mobile-Agent、MobiAgent、AppAgent、AutoGLM…… 从截图出发逐步驱动整套 UI。脆弱、慢、易被检测、法律灰色，而且**浪费**：它们要把登录、定位、支付状态这些 App 本就持有的东西重新跑一遍。

RelayAgent 背后的观察是：大多数超级 App **本身就已经内置了一个登录态的 AI 助手** —— 高德的助手 tab、微信里的元宝、淘宝/闪购的购物助手、小红书的点点、WPS AI。对相当大一部分真实意图来说，用户想要的能力*本就存在、本就登录、本就握着用户的上下文*，就在 App 里面。

所以我们主张**第三条路：把用户意图委派给 App 自己的内置助手** —— 而不是自己重做任务（纯 GUI 智能体），也不是等接口（A2A）。这条路需要的不是更聪明的自动化模型，而是一份*契约*：一个按 App 组织的发现层，告诉 OS 智能体哪些 App 内置了助手、它的输入框在哪、它能做什么，以及——最关键的——**什么时候必须把控制权交还给用户。**

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

## 演示

一段真机端到端运行：

**T1 —— 单 App 下单。** *「帮我点三杯蜜雪冰城蜜桃四季春，温度和糖度都用默认」* → 千问（Qwen）内置助手凑好 3 杯的购物车，停在支付页等用户确认。

![通过内置助手下单](assets/RelayAgentDemoOrder/RelayAgentDemoOrder.gif)

## 实测结果

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

**安全保障守住了。** 每一次 `handoff_to_user_required` 运行都停在了不可逆 CTA 之前——下单停在 `立即支付`——零次确认点击。冻结的 benchmark 目录中，7 张卡片的 **28/28 个能力**都到达了预期终态（§8.2.1）；当前 manifest 目录已扩展到 9 张卡片，Reddit Ask 和 Booking.com AI 聊天已单独真机验证。

> 上述数字为 2026-06-02 的 n=3 复跑结果；完整方法、有效性威胁与冻结数据见[技术报告](report/RelayAgent-TechReport.md)及 `report/benchmark-data-n3.md`。

## 仓库结构

```
RelayAgent/
├── SPEC.md                    # manifest 规范 (v0.1)
├── SPEC-OPEN-QUESTIONS.md     # 仍在讨论的设计问题
├── spec/schema.json           # SPEC 的 JSON Schema 镜像（规范性校验器）
├── manifests/                 # 每个 App 一张 YAML 卡片；10 张安卓卡片
├── agents/                    # 中继适配器、planner、能力路由、卡片加载器、adb 辅助
├── scripts/                   # run_plan.py（NL flow）、benchmark runner、metrics
├── docs/                      # 设计文档 —— NL flow、manifest 约定、prompt 模板、能力分类法
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
uv sync --no-install-project

# 2. 填好 .env（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL），然后跑一个目标
uv run python -m agents.runtime.native_runner com.aliyun.tongyi "帮我点三杯蜜雪冰城蜜桃四季春"
```

`agents.runtime.native_runner` 会 load `.env`、激活 AdbKeyboard 输入法、通过 `agents/runtime/_adb.py` 冷启动目标 App（force-stop + monkey LAUNCHER）、设置 `RELAY_SKIP_OPEN_APP=1` 让 planner 跳过自己的 `open_app` 步、在进程内直 adb 跑循环，并把额外 flag（如 `--max-step 40`）原样转发给 agent。不走 `.env` 时可用 `--model` / `--base-url` / `--api-key` 覆盖 LLM 配置。

`--model` 对各家通用——指向任意 OpenAI 兼容的 VLM 即可（`qwen/qwen3-vl-235b-a22b`、`anthropic/claude-sonnet-4-5`、`google/gemini-3`……）。每个任务里 VLM 用得很省（这正是 §8 成本数字的来源）：

- 1 次纯文本 LLM 调用，从卡片里挑出能力。
- 每个文本选择器先试 `uiautomator dump`（精确、零 token）；只有 miss 时才发一次小的 VLM grounding 调用。
- `wait_for_reply` **确定性**判定回复完成——无障碍树文本哈希连续多拍 byte-identical 才算 done——预算按墙钟（`max(5×typical_latency, 60)` 秒）。一个两阶段预检（截图感知哈希 → 无障碍树文本哈希）会在回复还在流式输出时跳过昂贵的 dump。**不用 VLM 判 done**。
- 回复文本**从无障碍 dump 抓取**；只有 scrape 落空（WebView/canvas 回复）时才让 VLM 读帧。对 `x_capture_full_reply` 能力，每一帧滚动抓取也是 scrape。
- 卡片里的 `screen_fraction` 坐标只在无障碍树暴露不出元素时作为最后兜底。

可选环境变量（完整列表见 `.env.example`）：

- `RELAY_MANIFESTS=/path/to/manifests` —— 覆盖默认的 `./manifests/`。
- `RELAY_PRECHECK=0 RELAY_SCRAPE=0` —— 关闭 §7 两项优化（复现基准 baseline）。
- `RELAY_TIMING=1` —— 写出每次运行的 `wall_clock.json`。
- `RELAY_FRESH_CONV=0` —— 跨 run 保留上一轮对话（默认每次开新对话）。
- `RELAY_ANDROID_SERIAL=...` —— 多设备时把每个 adb 调用钉到某一台。

适配器遵守 `handoff_to_user_required`：对任何不可逆能力，会在终态 CTA 之前发 `ask_user`，而不是自动确认。

### 自然语言入口

推荐用 `scripts/run_plan.py` 作为自然语言入口。它会合成 flow，对每个 app step 用共享的 matrix 三段式路由解析 app + capability，预览后执行；加 `--yes` 可跳过确认。

```bash
uv run python scripts/run_plan.py --yes "帮我点三杯蜜雪冰城蜜桃四季春"
uv run python scripts/run_plan.py --yes "帮我找一台适合学生的平板电脑，预算2000以内"
uv run python scripts/run_plan.py --dry-run "把这段材料整理成一份中文总结文档"
```

设计文档：

- [NL 跨 App Flow 架构](docs/nl_flow.zh.md) —— 合成、三段式路由、校验、执行、leg judge、handoff（pipeline 与 CLI 用法见[自动跨 App 规划器](docs/cross_app_planner.zh.md)）。
- [Manifest 约定](docs/manifest_conventions.zh.md) —— 语言约定、`prompt_template`、`x_capture_full_reply`、卡片 `swipe` 方向、capability 关键字段（[prompt 模板细节](docs/prompt_template.zh.md)）。

## 运行测试

```bash
uv sync --no-install-project
uv run python -m unittest discover -s tests -v       # 无设备；planner/runner 单元测试，不需要 adb
```

真机运行（不是单元测试）直接走入口脚本——见[运行](#运行真机多-vlm)：单 App 用 `python -m agents.runtime.native_runner <pkg> "<goal>"`，NL flow 用 `scripts/run_plan.py --yes`，A/B 基准用 `scripts/run_benchmark_test.py`。需要连好的安卓设备、装好目标 App，`com.android.adbkeyboard/.AdbIME` 可用（runner 自己启用/复位输入法）。

把 `.env.example` 复制成 `.env` 并填好（LLM endpoint 必填）。`test-results/` 和 `traj_logs/` 已 gitignore —— 不要提交含用户数据的轨迹。

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
- **接下来往哪走：** [产品化路线图](docs/roadmap.zh.md)——五个阶段（执行期失败恢复、流式抓帧、用户记忆、卡片 CI 与半自动生成、多平台），每阶段带验收指标，适合从这里认领工作。
- **提交卡片：** 见 [CONTRIBUTING.md](CONTRIBUTING.md)。
- **行为准则：** 见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
- **讨论：** GitHub Issues。

## 致谢

- [**MobiAgent**](https://github.com/IPADS-SAI/MobiAgent)（SJTU IPADS）—— 我们对标定位的纯 GUI 移动智能体路线，也是我们[技术报告](report/RelayAgent-TechReport.md)的结构范本。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。选宽松许可是为了便于企业采用——这套设计只有在手机 OEM 能无法律摩擦地采纳时才成立。
