# RelayAgent 评测设计（Evaluation）

> English: [`evaluation.md`](evaluation.md)

> 论文 Evaluation 章的设计与实现记录。决策日期 2026-06-09。
> 配套代码：`scripts/run_benchmark_test.py`（A/B driver + plan-only）、`scripts/eval/plot_eval_figs.py`（图）。

## 1. 一句话

在**同一台真机、同一个统一 VLM judge** 下，把同一批任务分别跑 **RelayAgent（relay）** 与 **MobileWorld `general_e2e`（baseline）**，按 **App 覆盖分层（covered / fallback）** 比较 **成功率 / 墙钟 / token / 步数**。核心论点：**RA 在它有专用 agent 覆盖的任务上大幅省时省 token 且成功率不弱于 baseline；覆盖不到时退化为 baseline、只交一点规划税。**

## 2. Baseline

- 主 baseline：**MobileWorld `general_e2e`**（通用 GUI agent，逐帧 pixel grinding），代表"不做专用路由、纯视觉操作"的范式。
- 关系澄清（重要）：**baseline 只是 RA 在 fallback 层的子集，不是 RA 整体的子集**。
  - **mw_fallback 层**：RA 把 unsatisfiable 的 leg 转 `type: mobileworld`，交给**同一个** general_e2e 执行 → 这层 baseline 是 RA 的子集。
  - **covered 层**：执行器换成专用 in-app agent（千问/高德/携程/…），与 general_e2e 是**两条不同路径（替换，非包含）**。
  - 推论：success 上 "RA ≥ MW" 是**经验期望、不是逻辑保证**——covered 层是不同执行器、可能逐任务更差；fallback 层还要减去**路由/handoff 误差**损耗。
- 计划中的额外对照：RA 消融（`RELAY_SCRAPE=0` / a11y agent，证明收益来自路由+scrape）；related-work 口径对齐 MobiAgent / Step-GUI。

## 3. 三个 benchmark（并列，不分主副）

| Benchmark | 来源 | 规模 | 语言 | 性质 / 作用 |
| --- | --- | --- | --- | --- |
| **RelayBench** | 自建 `benchmark/relaybench_tasks.yaml` | 30（15 single + 15 cross） | 中 | 覆盖 RA 的 10 个 manifest App，内部精确测量；风险=自建易被疑 cherry-pick |
| **AndroidDaily** | HF `stepfun-ai/AndroidDaily`（`Android Daily.csv`） | 235（以 single-app 为主） | 中 | 外部标准、中文日常 30+ App，重度命中 RA 覆盖（携程/高德/淘宝/饿了么/微信/小红书…）。**外部强证据** |
| **MobileWorld** | HF `Tongyi-MAI/MobileWorld` | 201 → **161（`--skip-mcp` 后）** | 英为主 | 外部标准、Mail/Mastodon/Files/Calendar/…，与 RA 覆盖低（仅 Maps/高德≈9）。作用=泛化/不退化 + cross-app 压力 |

- **MCP skip**：MobileWorld 里触及 `MCP-*`（Amap/arXiv/Github/stockstar/jina）的任务是 tool-call、非真 GUI；**全部是 cross-app（无纯 MCP 题）**，`--skip-mcp` 丢掉 40 条 → 161 条（85 cross + 76 single；144 en / 17 cn）。
- **AndroidDaily 指标错配**：其原生指标是对 ground-truth 轨迹的 **step-action-accuracy**；RA 路由到 in-app agent 不产出可比的 step 序列 → **只复用它的任务指令，用统一 e2e VLM judge 打分**（两系统同标尺）。

## 4. 核心轴：covered vs fallback

不按 benchmark 分主副，而是**在每个 benchmark 内部分层**（判定依据：planner 输出的每条 leg 的 **kind**——`specialized` 真垂类 capability / `foundation` 通用 `foundation_llm` / `mw` MobileWorld 兜底 leg）：

- **covered**：**每条** leg 都是 `specialized`（路由到专用 in-app agent）→ **省时省 token 的 headline 收益在这里**。
- **foundation_fallback**：无 MW leg，但有 ≥1 条 `foundation_llm` leg（只命中通用 QA）。
- **mw**：**每条** leg 都是 MobileWorld 兜底（== baseline 基座）→ RA 退化为 baseline，故事=**非退化 + 规划税**。
- **mixed**：MW leg + 非 MW leg 混合。`plan_summary.json["mw_fallback"]` 给该层（及全局）的 MW 占比：task 级 `task_touch_rate`、leg 级 `mw_leg_rate`、每条 mixed 任务的 `mixed_task_mw_ratios`。
- **invalid / error**：规划非法 / 网络等（注意 MW 兜底开着时 unsatisfiable / repair 用尽都转 MW，几乎不再产 invalid）。

RA 的 10 个手写 manifest：千问、高德、携程、微信、小红书、WPS、Booking、Reddit、Gemini、Copilot。**收益集中在这些 App** —— 用 covered 分层主动暴露收益来源，堵 cherry-pick。

## 5. 指标

1. **Completion rate**（统一 VLM judge `agents/leg_judge`，SUCCESS/total）—— 全量 + 分层。
2. **Wall-clock**（整任务子进程墙钟）—— **三套口径并报**（堵 selection-bias）：全量 / 各系统 completed-only / **两系统都成功的交集（配对）**。交集口径是 headline 效率数字（同题 RA/baseline 配对比值），但**不能单独报**——conditioning on baseline-success 会删掉"MW 超时跑不完、RA 几步完成"这类 RA 最大赢点，故必须与全量并列（全量含 MW 超时天花板，方向相反地高估 RA）。见 §9 fig6/fig7、fig5。
3. **Tokens**（prompt/completion/total）—— 口径同 wall-clock（全量 / completed-only / both-success 交集配对）。
4. **Steps / LLM 调用次数** —— covered 层最能讲"一次 submit 顶几十步 tap"。
5. **跨 App handoff 成功率**（单列，RA 差异化能力）。
6. **失败归因**（超时 / 路由错 / grounding 错 / handoff 误停）。

## 6. 时间/token 的分层预期（务必如实呈现）

- **covered 层**：规划税 + 一次廉价 in-app submit ≪ MW 几十步逐帧 → **RA 大幅赢**。
- **mw_fallback 层**：RA = 规划开销（+ coverage-gap 修复轮）+ **和 MW 一样**的执行 → **RA 略慢、token 略多、success ≈ MW**。这层是 RA 净交规划税，**如实画出来反而最显诚实**（见 fig5 TODO）。

## 7. Protocol / 诚实点（论文必须交代）

1. **self-judge**：judge 是 RA 自家 leg_judge → 抽 30–50 题人工核对，报一致率。
2. **relay token 口径（已修，task #8 ✓）**：relay 总 token 现读 `run_plan.py` 写的权威 `<flow_root>/token_usage.json`，`total` **已含 plan-synthesis 相**（+ 修复轮），`by_phase` 拆 plan/flow/agent → 规划税可直接量化（实测一条高德 POI covered 任务：plan 16975 / flow 698 / agent 0 token，规划相占绝大头）。**两侧 per-call 日志对齐**：results.jsonl 每行 `llm_calls` 放 per-call 指标（tokens+latency+model+purpose），完整正文（messages/response）落盘——relay 在各 leg `traj.json`、mw 经非侵入探针 `agents.llm.mw_llm_probe` 写 `<sys>/user_task/llm_calls.json`。
3. **completed-only 偏差**：现 `_aggregate` 时间/token 仅统计各系统自己完成的任务 → 必须并报全量 **+ 两系统都成功的交集配对**。三套口径方向相反地有偏（completed-only 各算各的、不可配对；全量含 MW 超时天花板→高估 RA；交集 conditioning on baseline-success→删掉 RA 最大赢点、低估 RA），故三者同列才诚实。**实现待办**：`_aggregate` 现按系统各自聚合，交集配对需按 `task_id` 求两系统成功交集再算 per-task ratio（不是均值相除）。
4. **测试公平性开关**：见 §8。

## 8. 测试时强制关闭的开关（公平 + 干净墙钟）

`run_benchmark_test.py` 在 arg-parse 后写 `os.environ`，relay 子进程与 in-process plan-only planner 都继承：

| 开关 | 默认 | 为什么测试要关 | 重开 |
| --- | --- | --- | --- |
| `RELAY_ROUTE_OVERLAY` | **0（关）** | 路由固化会让后续任务 0-LLM 查表短路 planner，跨任务泄漏热状态 → token/时间随顺序漂移、不公平 | `--route-overlay` |
| `RELAY_STEP_LOG` | **0（关）** | 每步写 PNG + tap/swipe 重编码标注帧，污染墙钟；traj.json 动作轨迹照常保留 | `--step-log` |
| `RELAY_CAPTURE_FULL_REPLY` | **0（关）** | MW 基线 `general_e2e` **无滚动捕获**——回复看着稳了就读当前可见帧文本再 `answer`；RA 的 `x_capture_full_reply`/`capture_full` 会把 offscreen 回复卡片滚进来再拼接，同一目标拿到严格更多回复内容 → 不公平。关掉后 `wait_for_reply` 在"屏幕文本 hash 稳定"判 done 后**直接返回首帧可见文本**，不进 scrolling 捕获相（gate 在 `relay_agent._materialize`：`capture_full = p["capture_full"] and self.capture_full_enabled`） | `--full-reply` |
| plan/route cache | **关**（relay 走 `--no-cache`） | 否则复用热 plan，省时虚高 | — |
| `--record` 录屏 | 不用 | 录屏后端额外开销 | — |

> 注：overlay 是 RA **真实提效特性**。默认关是为了公平逐任务对照；其"省了多少规划调用"应单独做 **overlay on/off 消融**（任务 #6，需 warm-up 预热 overlay 表后再测）。
> 保持开启（正确性相关，**别关**）：`RELAY_FRESH_CONV`、AdbKeyboard IME、cold-launch。

## 9. 图表集

代码：`scripts/eval/plot_eval_figs.py`，输出 `docs/eval_figs/{png,pdf}`。**数据当前为 MOCK**，schema 已对齐产出，换真值只动脚本顶部 `MOCK` 块。配色固定：relay 蓝 `#0072B2` / baseline 橙 `#D55E00`，covered 深绿、fallback 浅绿/紫。

- **Fig.1 覆盖分层**：每 benchmark 一条横向堆叠条（covered/foundation/mixed/mw/invalid），右标 N。数据 ← 各 `plan_summary.json["by_tier"]`。
- **Fig.2 covered 层效率**：covered 层 relay vs baseline 的 time/token/steps 三联屏，柱顶 `n×` 省幅，底部 success% 守门。数据 ← `summary.json`（covered 子集聚合）。
- **Fig.4 per-app dumbbell**：按 App 分类比 relay vs baseline 的 success/time/token；covered App 绿色加粗在上、fallback App 灰色在下（两点重合）；time/token 用 log 轴。数据 ← `summary.json["by_app"]`。
- **Fig.6 / Fig.7 配对散点（both-success 交集）**：`fig6_paired_tokens` / `fig7_paired_time`，每 benchmark 一个子面板（比值排序须在各自标尺内才有意义）。只画两系统都成功的题；横坐标无序号、按 `baseline/RA` 比值从大到小排（左=RA 赢最多），同题在同一 x 画蓝(RA)+橙(baseline)两点 + 细竖线连配对（**灰=RA 更省=covered 赢；红=RA 更贵=fallback 规划税**，直接把 §6 两层预期画进散点）；Y 轴 log；右上角标 median n× 与 RA wins%。covered 占比按各 benchmark plan-only covered_rate 设（RelayBench 高→几乎全灰几乎全赢；MobileWorld 低→几乎全红、median≈0.9×）。这是 §5.2 交集口径的散点底，比 fig2 柱状的均值 n× 更可信（看得到分布与反例）。数据 ← per-task join（`ra_ok/base_ok/ra_t/base_t/ra_k/base_k`）。
- **Fig.5（结果矩阵表）`fig5_outcome_table`**：2×2 success-outcome（RA × baseline），每 benchmark 一行 + TOTAL，四格 both succeed / `RA✓ base✗` / `base✓ RA✗` / both fail（+ N），配色绿/蓝/橙/红。两个 off-diagonal 是 discordant pairs → 可直接接 **McNemar 显著性检验**。数据 ← 同 fig6/7 的 per-task join。
- **Fig.3（CDF）/ Table2（消融表）已弃用。** **旧的"Fig.5 fallback 非退化 panel"已让位给上面的结果矩阵表；fallback 非退化展示并入 §6 口径，非重点。**

排版铁律：success 与效率永远同屏；三个 bench 同构复用一张图；报 median + 分布而非只均值；时间/token 给两套口径；颜色全篇统一。

## 10. 驱动实现地图（`scripts/run_benchmark_test.py`）

- `BENCHMARKS`：`mobileworld` / `relaybench` / `androiddaily`（loader + smoke picker；`single_app` 已删）。
- `--skip-mcp`：`_touches_mcp` 过滤触及 `MCP-*` 的任务。
- `--plan-only`：纯 LLM、无设备，按 leg kind 分 tier（covered / foundation_fallback / mw / mixed），输出 `plan_summary.json`（`by_tier` / `covered_rate` / `mw_fallback`{task+leg 级 MW 占比} / `covered_app_hits` / `covered_capability_hits`）。
- `_aggregate`（按系统）+ `_aggregate_by_app`（按 App×系统，喂 Fig.4）→ `summary.json` 的 `by_system` / `by_app`。
- 统一 judge：`_judge` 调 `leg_judge.judge_leg`，`loading` 重拍一次。

## 11. 当前数据状态

**plan-only 分类（新逻辑四档，全真值，2026-06-10）**——判定用 leg-kind（specialized/foundation/mw），见 §4：

| Benchmark | n | covered | foundation_fallback | mw | mixed | covered_rate | MW task占比 | MW leg占比 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| RelayBench | 30 | 27 | 3 | 0 | 0 | **0.90** | 0% | 0% |
| MobileWorld（skip-mcp） | 161 | 61 | 10 | 90 | 0 | **0.379** | 55.9% | 39.8% |
| AndroidDaily | 235 | 71 | 19 | 143 | 2 | **0.302** | 61.7% | 56.4% |

- 新逻辑（stage-3 逃生口）生效：MobileWorld 旧 foundation_fallback 102 → 现 **mw 90**（Gemini foundation 做不了的设备/OS 动作正确降级到 MW 兜底）；covered 61 含合法的 Gemini 邮件/日程/短信（manifest 真声明、matrix 唯一提供方）+ 3 条重跑新增。AndroidDaily 大量无 manifest 的 app（滴滴/京东/美团/拼多多/B站…）→ mw 143。
- 最终 covered ids（Phase B 真机集）：`traj_logs/reclassify/final/<bench>_covered_ids.txt`（27 / 71 / 61，合计 **159**）。

**真机 A/B（Phase B，进行中）**：对上面 159 个 covered case 跑 relay + mw general_e2e（日志已修，含 plan-synthesis token + 两侧 per-call）。relaybench 已完成 8/27，其余跑中（resume-aware：`scripts/eval/_phaseB_run.sh` 按 results.jsonl 断点续跑）。效率/成功率真值待跑完回填 Fig.2/4/6/7。

## 12. 未决 TODO

| # | 内容 | 性质 |
| --- | --- | --- |
| #6 | overlay on/off 消融（路由固化省多少规划调用；需 warm-up） | 实验 |
| #7 | fig5 fallback 非退化 panel（mock 先） | 展示（低优先） |
| #9 | 模型敏感性消融：从 baseline 的 success case 里挑 3 个，换更弱的模型重跑，看弱模型下是否转为失败（量化对底座模型质量的依赖） | 实验 |
| ~~#8~~ ✓ | ~~补采 `run_plan` plan-synthesis 的 token/耗时进 relay 侧~~ **已完成**：driver 读 `token_usage.json`（total 含 plan，by_phase 分相）；mw 探针补 per-call 正文 | 已修 |
| — | RA-native cross-app suite 真机覆盖确认（大部分 App 已装可跑） | 数据 |
| — | self-judge 人工核对子样本一致率 | 诚实性 |

## 相关记忆 / 文档

- 跨 App flow 架构：[`docs/nl_flow.zh.md`](nl_flow.zh.md)；capability 矩阵（真理来源）：`docs/app_capability_matrix.csv`。
- 路由固化 overlay：见 `nl_flow.zh.md` §9。
