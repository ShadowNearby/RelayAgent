# P1 执行期失败恢复 — mini-eval 记录（2026-07-08）

> 路线图 P1（[docs/roadmap.zh.md](../docs/roadmap.zh.md)）R0–R3 落地后的首次真机验证。
> **定位：机制演示，不是论文数字**——n=6、单轮、含一处评测中修复的 bug（见 §4），
> 正式的 R4 评测（三基准各 ~30 条、恢复开/关对照）另行安排。

## 1. 设置

- 实现：`8f6726c`（恢复梯子 R0–R3）+ `7764bc0`（leg judge live-frame 修复，评测中期合入，见 §4）。
- 任务集：6 条 **phase-B 首试失败**（`ra_success=0`）的 RelayBench 任务，全部只读
  （info_qa / content_gen；刻意避开 transaction 类与未安装的 Reddit）。
- 命令：
  ```bash
  uv run python scripts/run_benchmark_test.py --benchmark relaybench --systems relay \
      --all --ids-file <6 ids> --recovery --out-dir traj_logs/p1_recovery_eval
  ```
- 条件：benchmark 默认（`RELAY_ROUTE_OVERLAY=0` / `RELAY_STEP_LOG=0` /
  `RELAY_CAPTURE_FULL_REPLY=0`），恢复经 `--recovery` 打开（预算默认：每 leg 1 次重试、
  每 flow 2 条恢复 leg、15k token）。设备 Pixel 9（USB），模型 qwen（任务级 judge 同源）。
- 产物：`traj_logs/p1_recovery_eval/`（results.jsonl + 每任务 flow 根的
  `flow_report.json` / 原 leg 旁 `recovery.json`）。

## 2. 结果

| task | 任务级 verdict | 恢复活动 | tokens | wall(s) |
| --- | --- | --- | ---: | ---: |
| rb-x-gemini-wechat-01 | **success（翻盘）** | route_fail → retry(换措辞)→败 → reroute→无备选 → **MW 兜底→成功** | 47.7k（恢复 5.5k） | 327 |
| rb-x-copilot-wps-01 | **success（翻盘）** | 未触发（历史失败属抖动） | 35.6k | 154 |
| rb-sa-wechat-search-01 | loading | 未触发（leg judge 判 ok） | 20.7k | 56 |
| rb-sa-wechat-poi-01 | failure | 未触发（§4 修复前，judge 盲判） | 22.4k | 85 |
| rb-x-wechat-xhs-01 | failure | 未触发（leg judge 判 ok） | 24.9k | 114 |
| rb-x-amap-wechat-01 | failure | 未触发（leg judge 判 ok） | 33.0k | 147 |

**2/6 翻盘（其中 1 条纯靠梯子）；四档梯子在 gemini-wechat 上被完整行使一遍且全部按设计工作。**
此外实现当晚的一次非评测冒烟（Copilot leg 首试被通知权限弹窗卡死，judge 判 `app_error`，
重试翻盘）是另一例真实恢复，见 commit `8f6726c` 的说明。

## 3. 发现 1：梯子把环境故障绕过去了（正例）

gemini-wechat 的微信 leg 抓到的"回复"是一串 `sk-…` API-key 样式字符串（见 §5），
leg judge（live-frame）判 `route_fail` → 换措辞重试仍中同一故障（judge 这次判
`app_fail`，理由直接点名 "sent a technical API key string"）→ 换路由无备选卡 →
**MW 兜底完成检索**，answer 回灌 blackboard，任务级 judge 判整体 success。
恢复额外开销 5.5k token，在 15k 预算内。**用别的执行路径绕过单点环境故障**正是
梯子的设计目标，这条是教科书式的行使记录。

## 4. 发现 2：benchmark 条件曾让 leg judge 全盲（已修）

`RELAY_STEP_LOG=0`（benchmark 默认）下没有 `steps/` 截图，`final_frames()` 返回空，
leg judge 一律 "no frames to judge" → unknown → **judge 驱动的恢复档从不触发**。
修复（`7764bc0`）：无帧时对刚结束、App 仍在前台的屏幕抓一张实时帧来判——与既有
loading-retry 同一模式。前 2 条任务（wechat-search / wechat-poi）在修复合入前跑完，
其 leg "ok" 实为盲判；后 4 条在修复后运行。gemini-wechat 的翻盘正是该修复生效的直接结果。

## 5. 发现 3：设备端微信账号在漏 API key（环境故障，非 relay bug）

4 条失败任务里 **3 条**的微信 leg 抓到完全相同的 `sk-PqO0yXZ4…` 字符串
（poi-01、xhs-01 step2、gemini-wechat retry1）。这是这台测试机上微信 AI 搜索的
账号/服务端异常把 key 文本渲染进了回复区，回复 scrape 忠实地抓了屏幕上的内容——
路由、入口、scrape 本身都按设计工作。**后续**：处理测试机微信账号状态后重测这 3 条；
该串是服务端漏出的 key，不属于本仓库的泄露面。

## 6. 发现 4：leg judge 与任务级 judge 的精度差是下一个瓶颈

其余失败呈同一形态：**leg judge 判 ok（回复在题上），任务级 judge 判未达成**——
wechat-search 只列出 3/5 家（截断，`RELAY_CAPTURE_FULL_REPLY=0` 下只取首屏文本）；
amap-wechat 第二 leg 回答"附近无 24h 药店、建议自查"，在题但没满足"核实营业状态"。
**恢复只能救 leg judge 能识别的失败**；把任务约束（数量、字段完整性）喂进 leg judge
的判据，属于 judge 精度轴，是 P1 之后成功率的下一个杠杆（并与 §5 环境修复叠加）。

## 7. 结论与后续

- 梯子机制验证通过：分类、四档次序、预算、安全红线（handoff 只重试）均按设计工作，
  且有两例真实恢复（权限弹窗→重试；环境故障→MW 兜底）。
- 本轮 6 任务 0%→33%；样本太小，只作机制证据。
- 后续（按优先级）：① 修测试机微信账号后重测 §5 的 3 条；② leg judge 吃任务约束
  （judge 精度轴）；③ 正式 R4：三基准各 ~30 条、`--recovery` 开/关对照、
  逐档命中率与 token 通胀表。
