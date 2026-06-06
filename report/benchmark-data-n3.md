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
- **wall_s 仅作记录、baseline↔opt 不作墙钟对比**（见 finding 2）。opt 三次 wall
  48.5/51.7/**68.9** 的方差几乎全来自那唯一一次 confirm-VLM 的网关延迟 —— VLM 总时间
  4.8/7.9/**24.6**s 与 wall **一一对应**（r3 多花的 ~17s 墙钟 ≈ 多花的那 ~17s VLM 秒数），
  说明 agent 可控部分与 baseline 相同、残差是 serving 栈抖动。precheck 把 VLM 调用次数从
  baseline 的 3-4 砍到恒定 2，收益记在 **token / VLM 往返次数**轴。（上一轮 opt 中位 74.1
  是两次慢-VLM 跨 session 抽样的产物，交错重测回落到 51.7。）
- baseline VLM 次数 3/4/4（每多一次 done-poll 多 ~3k token），token 6.8k–9.6k；VLM 总时间
  16.7/7.2/13.1s（每次 done-poll 都打满 VLM 往返，总 VLM 时间反而常高于 opt）。
- **general_e2e 在 order_food 上方差大**（38282/77347/96888 token、5/9/11 步、
  46/111/379s，2.5× token / 8× wall 跨度）：**三次都成功**——都到订单确认/付款页、
  购物车已组好（3 杯默认规格）、停在支付前。差异来自助手每次弹出多少张中间选择卡片
  要 agent 逐张点（5/9/11 步）+ 每步网关延迟，是**成功之间的方差**而非成败之差
  （已逐帧核对 r1/r2/r3 轨迹：r2=38282 是助手一轮直接组好购物车、agent 无中间卡可点）。
  更剧烈的失败模式（撞千问残留会话提前退出 / 失控循环到 50 步/531k）是早期 n=1 探索
  里见到的，**不在本 trio**。干净成功路径同样可参考 `benchmark-data-n1.md` 的 38081 tok。

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

> **flow opt 复现（2026-06-02，`test-results/ab/n3_retest/flow_optimized_r*`，仅重测 opt）**：
> token 合计 5698 / 8615 / 11217（中位 **8615**，对原 8662 几乎逐字复现，3.6× vs baseline 成立）；
> wall 合计 125.7 / 160.8 / 148.4（中位 **148.4**，对原 153.5 复现）；ride 腿照旧极稳（2809×2）。
> **结论**：opt 自身 token/wall 高度可复现。**flow 的 opt-vs-baseline 墙钟对照沿用上一轮**
> （opt 153.5 vs baseline 115.8）—— 本次只重测 opt、未同 session 重测 baseline，故不对 flow 的
> 「opt≈baseline 墙钟」下新结论（order_food 的同 session 交错对照已单独验证该结论）。

> flow 的「VLM 总时间」无法从现有 run 目录提取：旧版多 app 任务分腿走子进程 `mw test`
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

2. **wall-clock 只在「重驱动 → 委派」粗梯度上比，baseline↔opt 不比墙钟**（编辑决策）。
   - **比墙钟的三档**：MW manual-UI → MW general_e2e → RA（用 baseline 作 RelayAgent 代表）。
     这里时间节省真实且大（order_food 193 → 111 → 47.6s；flow 717 → 166 → 115.8s），
     因为每步**实际做的事更少**（手滚笔记 23 步 → 一次助手对话；逐帧重推 → 结构化 plan +
     0-token a11y 点击）。e2e 慢跑也更慢（order_food 379s = 11 步成功跑被每步网关延迟拖长，非死循环）。
   - **baseline↔opt 之间故意不比墙钟**：那段差异由**单次 confirm-VLM 的网关延迟**主导
     （`V` 在共享网关上 1.4–32s 抖），是 **serving 栈属性、不是 agent 设计属性**。
     order_food 同 session 交错重测坐实了这点：opt 的 VLM 总时间 4.8/7.9/24.6s 与 wall
     48.5/51.7/68.9 **一一对应** —— 唯一推动墙钟的就是那次 VLM 的网关抽样，agent 可控部分
     与 baseline 相同。所以 precheck/scrape 的收益只在 **token / 调用次数**轴上报（§节省幅度），
     在这条轴上谈墙钟差异 = 报告网关噪声。（佐证：上一轮 opt 中位 74.1 偏高纯是两次慢-VLM
     跨 session 抽样的产物，交错重测回落到 51.7、与 baseline 齐平。）
   - 注：perf-trim（sleep 削减）是对**同一配置固定 per-tick 开销**的 before/after 提速
     （order_food 冷启动→handoff ~70s→~51s），削的是 agent 可控部分、独立于上面的网关 VLM
     时间，**不是** baseline-vs-opt 主张，勿混。

3. **RelayAgent 可预测，纯 VLM agent run-to-run 方差大**。RA 每档三次近乎复现
   （order_food token 3950–3987，1.01×）；general_e2e order_food 三次 38282/77347/96888
   token、46/111/379s（2.5× token / 8× wall），虽三次都成功但成本随助手中间卡片数 + 网关
   抖动大幅波动；更剧烈的早退/失控模式见早期探索。可预测的成本本身是 system contribution。

## 附：token 计价 + 历史图差分（2026-06-03 补）

**prompt/completion 拆分**（现有 traj.json 直接读，零重跑）：全配置 ~96–99% prompt
token，completion <2%。单图 ≈ **2783 prompt token**（RA 的 reply_watch 一张图）。
RA optimized = 1054（文本 router）+ 1×2783 = 3987，恰好 1 张图；baseline 1 router +
2–3 张；general_e2e/manual 每步 1 张 + 3 张历史 ≈ 9 步 30 张。**19× ≈ 1 张图 vs ~30 张**。

**美元换算**（OpenRouter 公开价 `qwen3.5-27b`：$0.195/M in、$1.56/M out，2026-06-03
联网核实，含 35% 促销折扣但结论只依赖 1:8 比值）：RA opt $0.00098 / baseline $0.0021
(2.2×) / general_e2e $0.0163 (16.6×) / manual $0.0158 (16.1×)。**$ gap 16.6× 略小于
token gap 19.4×**（~14% 压缩，源于 1:8 比 + RA opt completion 占比略高）。详
`report/cost-dollar-analysis.md`。

**HISTORY_N_IMAGES=1 差分**（`test-results/ab/n3_hist1/`，T1 n=3，砍掉 3 张历史图只留当前帧）：

| 条件 | steps | prompt | total | prompt/step | 成功 |
|---|---:|---:|---:|---:|---|
| 1img r1 | 4 | 15521 | 15789 | 3880 | ✅ 到付款页 |
| 1img r2 | 50 | 327245 | 332100 | 6545 | ❌ 卡数量选择器 |
| 1img r3 | 50 | 328326 | 333228 | 6567 | ❌ 卡数量选择器 |
| 3img(n3) 均值 | — | — | — | **8259** | 3/3 ✅ |

- 3 张历史图税 ≈ **4378 prompt token/step ≈ 53%**，单帧 ≈ 1.8–2.2k token/step。
- **但砍历史图不是更省的 baseline**：成功率 3/3 → **1/3**（丢帧丢了任务关键的前态上下文，
  agent 在数量选择器上死循环），两次失败烧 **~332k token = 3img 的 4×**；唯一成功那次
  (15789) 仍是 RA optimized 的 ~4×。→ **3-image 是 general_e2e 的承重工作配置、非注水**,
  RA 的差距不是"比了个过重 baseline"的产物。换**输入模态**(a11y-text/SoM)仍未测。

## 附：RELAY_NO_MANIFEST 消融（manifest-isolation，2026-06-03 补）

**目的**：拆开 Q2 的 19×——到底是「委派(delegation)」还是「手写 manifest」的功劳。
新建 adapter 模式 `RELAY_NO_MANIFEST=1`（`agents/relay_agent.py`：**不加载任何 card**，
驱动同一委派骨架——开新对话→把整条用户请求一次性打进助手→wait_for_reply→accept-defaults
推进→不可逆 CTA 前 handoff——但入口/输入框/发送/推进/CTA stop **全部运行时 VLM grounding**，
唯一用到的 app 事实是包名，跟 general_e2e 一样）。T1 order_food，n=3，`test-results/ab/nm/`。

| run | total | prompt | compl | VLM calls | 结果 |
|---|---:|---:|---:|---:|---|
| nm r1 | **14147** | 13958 | 189 | 5 | ✅ clean：助手一轮组好购物车→付款页，nm_advance 测到 `支付宝付款` CTA→handoff |
| nm r2 | 14105 | 13958 | 147 | 5 | ❌ fail：输入框被 VLM 误定位到屏幕上部，查询没发出去，停在主页 |
| nm r3 | 17141 | 16929 | 212 | 6 | ◐ partial：发出查询→选店页→点「选这个」→停在付款前(done) |

**token 拆解（用 r1 clean，单图 ≈2783 prompt）**：nm = 5 次 VLM image（3 grounding +
1 reply_watch + 1 advance，**无 router**）≈ 14k。排序正好是预测的
**RA 3987 < no-manifest 14147 < general_e2e 77347**：
- general_e2e → no-manifest = **5.5×**（杠杆 **B**：结构化委派骨架替代逐帧自由重驱动，~30 图→5 图）
- no-manifest → RA opt = **3.5×**（杠杆 **C**：manifest 的 0-token uiautomator 点击替代 ~4 次 VLM grounding + 省掉 advance 探针）
- → **19× 主要是 delegation（B 更大），manifest 是真实但次要的优化** —— 实测支撑 §8.2 归因。

**可靠性 1/3（重要 nuance）**：只有 r1 干净到付款；r3 接上了订单流程但提前停（安全但没到付款）；
r2 的输入框 grounding 落错区域、查询没发。同一 prompt r1 定位对、r2 定位错——CN UI 上纯 VLM
grounding 不稳。→ **manifest 不只省 token，还买可靠性**（正是它把这些 affordance 编码成
selector/bounds 的理由），与 HISTORY_N_IMAGES 实验同性质。**未加 uiautomator 拐杖**（那会让 nm
滑向 RA、抹掉杠杆 C 的隔离），纯 VLM grounding 才是 §8.9 #1 定义的忠实 ablation。

## 备注
- RA 各档 wall_s 由 `RELAY_TIMING=1` 写入各 run 的 `wall_clock.json`；general_e2e 由
  `run_n3.sh` 的 `run_e2e()` 手动计时。
- 「VLM 总时间」（`vlm_s`）= 单 app run 的 traj 里每条 `llm_calls[].elapsed_s` 之和，由
  `aggregate_metrics.py` 汇总；只在 order_food 这类单 app run 可得，flow 子run 不写
  per-call elapsed（见 flow 节）。order_food opt/baseline 取 `test-results/ab/n3_retest/`
  重测轮，其余取原 `test-results/ab/n3/`。
- 本轮全部 self-start server（不带 `--aw_host`），靠 fork 的 PIPE-死锁修复才能稳定跑完
  长批次（上一轮该路径会中途偶发冻死）。
