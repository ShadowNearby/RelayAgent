# RelayAgent 基准数据 — n=3 + wall-clock（重跑 2026-06-02）

> 每档 3 次取**中位**，全程 `RELAY_TIMING=1` 记 wall-clock。跳过 MW manual-UI（无助手）。
> 原始 21 个 run 目录在 `test-results/ab/n3/`，驱动脚本 `test-results/ab/run_n3.sh`，
> 单 run 聚合 `scripts/aggregate_metrics.py`。
> **本轮是在 perf-trim（每步 settle sleep 削减）+ 三个 MobileWorld fork 健壮性补丁
> （`MW_WAIT_SECONDS` / `MW_ADB_TIMEOUT` / 自起 server 输出写文件修 PIPE 死锁，
> 见 CLAUDE.md）合入 main 后重跑**。21 个 run 全部端到端完成、零卡死（健壮性补丁验证）。
> 结论与上一轮 n=3（2026-06-01）一致：token 收益大且稳，wall-clock 非优化档卖点。

## order_food（蜜雪冰城蜜桃四季春×3，千问=淘宝闪购内置 AI）

| 配置 | token 中位 | token 三次 | VLM 中位 | wall_s 中位 | 完成情况 |
|---|---:|---|---:|---:|---|
| **RA optimized** | **3989** | 3989 / 3950 / 3989 | 2 | 74.1 | ✅ 三次都到 handoff，**极稳**（token±20） |
| **RA baseline** | **9566** | 6790 / 9566 / 12423 | 4 | 47.6 | ✅ 三次都到 handoff |
| MW general_e2e | ⚠️ 不可靠 | 38282 / 77347 / 96888 | — | 46 / 111 / 379 | ⚠️ 方差极大，见下 |

- RA optimized 三次几乎复现（3989/3950/3989，VLM 恒为 2 = router1+reply1）。
- baseline VLM 次数 5/3/4 抖动（每多一次 done-poll 多 ~3k token），token 6.8k–12.4k。
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

## 节省幅度（n=3 中位）

| 链路 | RA opt vs RA baseline | RA opt vs general_e2e |
|---|---:|---:|
| order_food | **−58.3%**（2.4×） | **−94.8%**（19.4×，e2e 用中位 77347） |
| flow | **−72.2%**（3.6×） | **−90.9%**（11.0×） |

## 三个核心 finding

1. **token 节省大且稳**。RA optimized vs baseline：order_food 3989 vs 9566（2.4×）、
   flow 8662 vs 31174（3.6×）；vs 纯逐步 VLM（general_e2e）order_food 19×、flow 11×。
   优化档三次方差极小（order_food 3989±20）。

2. **⚠️ wall-clock 不是 RA-baseline→optimized 这一档的卖点**（诚实结论，n=3 复现）。
   两档 wall_s 在噪声内、优化档甚至更高：order_food 74.1 vs 47.6、flow 153.5 vs 115.8。
   原因：**墙钟由 app 内助手的生成延迟 + LLM 网关单次延迟主导**（optimized 的单次
   done-confirm VLM poll 在不同 rep 耗时 6s–43s，纯网关抖动），precheck/scrape 砍的是
   **VLM 调用次数 / token**，不是助手等待时间。
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
- 本轮全部 self-start server（不带 `--aw_host`），靠 fork 的 PIPE-死锁修复才能稳定跑完
  长批次（上一轮该路径会中途偶发冻死）。
