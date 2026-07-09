<h1 align="center">RelayAgent</h1>

<p align="center">
  <b>通过将子任务委派给 App 内置助手，实现移动端任务自动化</b>
</p>

<p align="center">
  <a href="#-引用">📚 论文</a> •
  <a href="android/README.md">📱 Android App</a> •
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

RelayAgent 是一个移动端 Agent：它通过把子任务**委派给 App 内置助手**（In-App AI Assistant，例如千问助手、小红书点点、微信 AI 助手）来完成整个任务。

<p align="center">
  <img src="assets/paper/arch.png" alt="RelayAgent 架构：一条自然语言请求被分解为子任务，每个子任务委派给合适的 App 内置助手" width="820">
  <br>
  <em>RelayAgent 架构：先将任务拆解，再把每个子任务委派给对应的 App 内置助手</br><b>找一家高分的附近餐厅</b> → 小红书助手；<b>导航前往</b> → 高德地图助手</em>
</p>

---

## 📖 简介

RelayAgent 由三个部分组成：

- **🧭 基于委派的核心执行流** 将任务分解为 App 内可完成的子任务，每个子任务路由给相应 App 内置助手执行；若无助手可完成，则由 GUI Agent 通过截图逐步兜底。
- **📚 动态能力卡（Capability Card）** 将 App 内置助手建模为能力卡（YAML manifest），描述调用方式与能力边界，据此进行子任务路由。
- **🧪 RelayBench** 一个自建的端侧 Agent 测试集，包含 30 个长时序日常任务，重点覆盖现有测试集未涉及的 App 及其功能。

### 与 API 方案和 GUI Agent 方案的对比

|  | 厂商 API<br>(A2A / App Intents / AppFunctions) | 纯 GUI Agent<br>(Mobile-Agent、AppAgent…) | RelayAgent（本项目） |
| --- | --- | --- | --- |
| 谁来做任务 | App 暴露的 API | 由 Agent 驱动的用户模拟行为 | App 内置助手 + 由 Agent 驱动的用户模拟行为 |
| 需要厂商配合 | 必须 | 不需要 | 不需要 |
| 覆盖面 | 窄（只有已发布的接口） | 任意 App | 任意 App |
| 单个 App 内任务的成本/时延 | 低 | 高 | 低 |

<p align="center">
  <img src="assets/paper/gui-agent.png" alt="纯 GUI Agent：每步一次截图 + 一次 VLM 往返" width="720">
  <br>
  <em>纯 GUI Agent：每步一次截图 + VLM 往返</em>
  <br><br>
  <img src="assets/paper/dele-agent.png" alt="RelayAgent 委派：能力卡给出确定性入口脚本，App 内置助手执行任务" width="720">
  <br>
  <em>RelayAgent：将任务委派给 App 内置助手</em>
</p>

## 📊 测试结果

RelayAgent 在三个测试集（AndroidDaily、MobileWorld、RelayBench）上均优于纯 GUI Agent 基线（MobileWorld 的 `general_e2e`）：

- 成功率提高 6–15 个百分点
- 端到端速度约为基线的 1.4–2 倍
- token 消耗约为基线的 1/7–1/10

<p align="center">
  <img src="assets/paper/fig1_completion_bars.png" alt="成功率：RA 46/49/83% vs 基线 GUI Agent 31/34/77%（AndroidDaily / MobileWorld / RelayBench）" width="820">
  <br>
  <em>任务完成率。浅蓝 = 无 Fallback 的 RelayAgent，蓝 = RelayAgent，橙 = GUI Agent Baseline</em>
  <br><br>
  <img src="assets/paper/fig2_paired_time.png" alt="逐任务完成时间，RA（蓝）vs 基线（橙），按任务配对；阴影区为通过 GUI Agent 兜底完成的任务" width="820">
  <br>
  <em>任务完成时间。蓝 = RelayAgent，橙 = GUI Agent Baseline；阴影区为通过 GUI Agent 兜底完成的任务。</em>
  <br><br>
  <img src="assets/paper/fig3_paired_tokens.png" alt="逐任务 token 消耗，RA（蓝）vs 基线（橙），按任务配对；阴影区为通过 GUI Agent 兜底完成的任务" width="820">
  <br>
  <em>任务 token 消耗</em>
</p>

## 🎬 演示

*「帮我点三杯蜜雪冰城蜜桃四季春，温度和糖度都用默认」* → 将任务委派给千问 App 内置助手，助手帮助完成点外卖，完成后停在支付页。

<p align="center">
  <img src="assets/RelayAgentDemoOrder/RelayAgentDemoOrder.gif" alt="经 App 内置助手下单，支付前停住" width="320">
</p>

## 🚀 快速开始

开启 USB 调试的安卓手机（当前能力卡仅支持 Google Pixel 9）或模拟器，需安装并启用 ADB Keyboard 输入法（`com.android.adbkeyboard/.AdbIME`）。详见 [真机配置](docs/device_setup.zh.md) 和 [模拟器测试](docs/emulator_testing.zh.md)。

```bash
git clone https://github.com/ShadowNearby/RelayAgent.git && cd RelayAgent
uv venv --python 3.12 && uv sync --no-install-project --extra dev
cp .env.example .env
# 修改 .env 中的 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
uv run python scripts/validate/check_device_env.py    # 检查运行环境
uv run python scripts/run_plan.py --yes     "帮我点三杯蜜雪冰城蜜桃四季春"
```

### 运行测试

```bash
uv run python -m unittest discover -s tests -v            # 无设备；单元测试
uv run python scripts/run_benchmark_test.py               # 有设备；运行端到端测试，包含 Baseline 和 RelayAgent
```

## 📱 Android App

将规划、路由、执行、日志全流程运行在**Android App** 里，见 [`android/`](android/README.md)。

<p align="center">
  <img src="assets/android/home_zh.png" alt="RelayAgent Android App 会话式任务流首页" width="300">
</p>

## 📚 文档

- [核心流程的架构](docs/nl_flow.zh.md)
- [能力卡约定](docs/manifest_conventions.zh.md)
- [真机测试环境搭建](docs/device_setup.zh.md) / [模拟器测试环境搭建](docs/emulator_testing.zh.md)
- [路线图](docs/roadmap.zh.md)
- [Android App](android/README.md)

## 📇 支持的参考能力卡

10 个 App · 50 个 App 内置助手能力（高德、通义千问、携程、Gemini、小红书、微信、WPS、Reddit、Booking.com、Microsoft Copilot）。详见 [docs/cards.zh.md](docs/cards.zh.md)。

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

- [**MobileWorld**](https://github.com/Tongyi-MAI/MobileWorld)（Tongyi MAI）—— 所用测试集之一，也是 GUI Agent Baseline 的来源。
- **AndroidDaily** —— 所采用的测试集之一。

## 📄 [LICENSE](LICENSE)
