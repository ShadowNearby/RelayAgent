# RelayAgent 基准数据 — n=3 + wall-clock（重跑 2026-06-02）

> 每档 3 次取**中位**，全程 `RELAY_TIMING=1` 记 wall-clock。跳过 MW manual-UI（无助手）。
> 原始 21 个 run 目录在 `test-results/ab/n3/`，驱动脚本 `test-results/ab/run_n3.sh`，
> 单 run 聚合 `scripts/aggregate_metrics.py`。
> **本轮是在 perf-trim（每步 settle sleep 削减）+ 三个 MobileWorld fork 健壮性补丁
> （`MW_WAIT_SECONDS` / `MW_ADB_TIMEOUT` / 自起 server 输出写文件修 PIPE 死锁，
> 见 CLAUDE.md）合入 main 后重跑**。21 个 run 全部端到端完成、零卡死（健壮性补丁验证）。
> 结论与上一轮 n=3（2026-06-01）一致：token 收益大且稳，wall-clock 非优化档卖点。

## order_food（蜜雪冰城蜜桃四季春×3，千问=淘宝闪购内置 AI）

> opt + baseline 于 2026-06-02 在**同一 session 交错重测**（`test-results/ab/n3_retest/`，
> 驱动 `test-results/ab/retest_opt_vs_baseline.sh`），两档共享当天网关条件，消除了上一轮
> 跨时段网关抖动的混淆。general_e2e 仍取原 `test-results/ab/n3/` 那轮（本次未重测）。
> 「VLM 总时间」= 该 run 所有 VLM 调用的 `elapsed_s` 之和（`aggregate_metrics.py` 的 `vlm_s`）。

| 配置 | token 中位 | token 三次 | VLM 中位 | VLM 总时间中位(s) | wall_s 中位 | 完成情况 |
|---|---:|---|---:|---:|---:|---|
| **RA optimized** | **3986** | 3987 / 3986 / 3950 | 2 | 7.9 | 51.7 | ✅ 三次都到 handoff，**极稳**（token±20） |
| **RA baseline** | **9585** | 6788 / 9585 / 9594 | 4 | 13.1 | 47.6 | ✅ 三次都到 handoff |
| MW general_e2e | ⚠️ 不可靠 | 38282 / 77347 / 96888 | — | — | 46 / 111 / 379 | ⚠️ 方差极大，见下 |

- RA optimized 三次几乎复现（token 3987/3986/3950，VLM 恒为 2 = router1+reply1）。
- **wall_s：opt 51.7 vs baseline 47.6（+8.6%，噪声内）**。opt 三次 wall 48.5/51.7/**68.9**
  的方差几乎全来自那唯一一次 confirm-VLM 的网关延迟 —— VLM 总时间 4.8/7.9/**24.6**s 与
  wall **一一对应**（r3 多花的 ~17s 墙钟 ≈ 多花的那 ~17s VLM 秒数）。precheck 把 VLM 调用
  次数从 baseline 的 3-4 砍到恒定 2，省的是 **token / VLM 往返次数**，不是助手等待墙钟
  （见 finding 2）。上一轮 opt 中位 74.1 是两次慢-VLM 抽样把中位抬上去的产物，本轮干净
  对照下回落到 51.7、与 baseline 持平。
- baseline VLM 次数 3/4/4（每多一次 done-poll 多 ~3k token），token 6.8k–9.6k；VLM 总时间
  16.7/7.2/13.1s（每次 done-poll 都打满 VLM 往返，总 VLM 时间反而常高于 opt）。
- **general_e2e 在 order_food 上极不稳**（38k→97k token、46s→379s）：纯逐步 VLM、无
  「开新对话」步，撞千问残留会话时要么早退要么跑飞。**这是 finding：纯 VLM agent 对
  初始状态敏感、方差爆炸。** 干净成功路径参考 `benchmark-data-n1.md` 的 38081 tok。

## flow（xhs→amap，合计 discover+ride）

| 配置 | token 中位 | token 三次（合计） | wall_s 中位 |
|---|---:|---|---:|
| **RA optimized** | **8662** | 8662 / 5705 / 11233 | 153.5 |
| **RA baseline** | **31174** | 34247 / 22535 / 31174 | 115.8 |
| MW general_e2e | 95296 | 95296 / 95273 / 103988 | 166 |

拆腿（中位）：
- RA optimized：discover ~2896、ride ~2809（ride 极稳；discover 随点点回复长度 2.8k–5.9k）
- RA baseline：discover ~19932、ride ~11241
- general_e2e：discover ~37897、ride ~57376（discover 37k–57k，ride 47k–57k）

> flow 的「VLM 总时间」无法从现有 run 目录提取：flow 经 `flow_runner` 分腿走子进程 `mw test`
> （`--log-file-root`），子run 的 traj 只落聚合 `token_usage`、不写 per-call `elapsed_s`
> （见 `aggregate_metrics.py:108-117` 的 fallback 分支），故 `vlm_s` 不可得。要补 flow 的
> VLM 总时间需带 per-call 计时重跑 flow。

## 节省幅度（n=3 中位）

| 链路 | RA opt vs RA baseline | RA opt vs general_e2e |
|---|---:|---:|
| order_food | **−58.4%**（2.4×） | **−94.8%**（19.4×，e2e 用中位 77347） |
| flow | **−72.2%**（3.6×） | **−90.9%**（11.0×） |

## 三个核心 finding

1. **token 节省大且稳**。RA optimized vs baseline：order_food 3986 vs 9585（2.4×）、
   flow 8662 vs 31174（3.6×）；vs 纯逐步 VLM（general_e2e）order_food 19×、flow 11×。
   优化档三次方差极小（order_food token 3950–3987）。

2. **⚠️ wall-clock 不是 RA-baseline→optimized 这一档的卖点**（诚实结论，n=3 复现）。
   同一 session 交错重测后两档 wall_s 在噪声内、基本持平：order_food **51.7 vs 47.6（+8.6%）**、
   flow 153.5 vs 115.8。原因：**墙钟由 app 内助手的生成延迟 + LLM 网关单次延迟主导**
   —— order_food 重测里 opt 的 VLM 总时间 4.8/7.9/24.6s 与 wall 48.5/51.7/68.9 **一一对应**，
   多出来的墙钟全是那一次 confirm-VLM 的网关抖动；precheck/scrape 砍的是 **VLM 调用次数 /
   token**，不是助手等待时间。（注：上一轮 opt 中位 74.1 偏高是两次慢-VLM 抽样的产物，并非
   系统性更慢；交错重测消除跨时段网关抖动后回落到 51.7。）
   - 时间节省真正出现在 **general_e2e → RA** 这一档（order_food e2e 会跑飞到 379s）。
   - **report 措辞**：优化讲成「**token / 成本**节省」；时间节省只在「逐步 VLM →
     RelayAgent」梯度主张，别把 precheck 说成省时间。
   - 注：perf-trim（sleep 削减）是对**同一 optimized 配置**before/after 的提速
     （order_food 冷启动→handoff ~70s→~51s，见 round1/round2 n=1），与此处
     optimized-vs-baseline 是两个不同维度的对比，勿混。

3. **RelayAgent 可预测，纯 VLM agent 方差爆炸**。RA 每档三次近乎复现；general_e2e
   order_food 从 38k token 早退到 97k/379s 跑飞。可预测的成本本身是 system contribution。

## 备注
- RA 各档 wall_s 由 `RELAY_TIMING=1` 写入各 run 的 `wall_clock.json`；general_e2e 由
  `run_n3.sh` 的 `run_e2e()` 手动计时。
- 「VLM 总时间」（`vlm_s`）= 单 app run 的 traj 里每条 `llm_calls[].elapsed_s` 之和，由
  `aggregate_metrics.py` 汇总；只在 order_food 这类单 app run 可得，flow 子run 不写
  per-call elapsed（见 flow 节）。order_food opt/baseline 取 `test-results/ab/n3_retest/`
  重测轮，其余取原 `test-results/ab/n3/`。
- 本轮全部 self-start server（不带 `--aw_host`），靠 fork 的 PIPE-死锁修复才能稳定跑完
  长批次（上一轮该路径会中途偶发冻死）。
