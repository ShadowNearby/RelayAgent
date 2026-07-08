# 产品化路线图 — 从"论文可测"到"天天可用"

> English: [roadmap.md](roadmap.md)
>
> 本文是面向贡献者的工程路线图:按投入产出排序的五个阶段,每阶段锚定到现有代码接缝,并用仓库自带的 A/B 评测基建验收。讨论与认领见 GitHub Issues。

## 现状基线(为什么是这个优先级)

内部 phase-B 真机 A/B(186 任务,初步数据,人工判读混合)给出的端到端成功率:**RelayBench ~67% / AndroidDaily ~51% / MobileWorld ~42%**;任务中位墙钟 75–128s。结论:

1. **可靠性是最大短板**——失败即 raise、无任何执行期恢复,而恢复所需的原材料(leg 判决、blackboard、MW 转换、路由固化)全部已存在;
2. **延迟瓶颈在 ~1.2s/帧的 screencap 与围绕它的固定 sleep**,不在框架;
3. 记忆、卡片规模化、多平台依次排后:没有 ① 的成功率,其余投入都在漏水的桶里注水。

| 阶段 | 内容 | 规模 | 验收指标(基线 → 目标) |
| --- | --- | --- | --- |
| P1 | 执行期失败恢复闭环 | ~3 周 | AndroidDaily 51%→70%+,MobileWorld 42%→60%+,RelayBench 67%→85% |
| P2 | 流式抓帧 + 延迟工程 | ~2 周(可与 P1 并行) | 单步非 LLM 开销 ~2.5s→<0.5s;任务中位墙钟降 ~30% |
| P3 | 用户记忆层 | ~2 周 | 隐式偏好任务的 ask_user 轮数减半 |
| P4 | 卡片回归 CI + 半自动生成 | CI 1 周 + 录制器 3 周 | 卡片失效发现:出事才知道 → 24h 内自动 issue |
| P5 | 多平台 / OEM | 长线并行 | 里程碑制(见下) |

---

## P1 执行期失败恢复闭环(~3 周)

> **状态(2026-07-08)**:R0–R3 已实现(`agents/flow/leg_recovery.py`,nl_flow §6.1),mini-eval 见 [`report/p1-recovery-mini-eval.md`](../report/p1-recovery-mini-eval.md)——四档梯子真机完整行使一遍并翻盘一条历史失败任务。R4 **遥测侧已收口**:`recovery.json` 每条尝试带 token 成本;`run_benchmark_test.py --recovery` 每行落 `recovery` 块,`summary.json`/`summary.md` 出首试 vs 最终成功、逐档命中率与恢复 token 通胀表(`tests/test_benchmark_recovery.py` 钉住)。R4 正式评测(三基准各 ~30 条开/关对照)待跑。

**P1 之前的现状**:`flow_runner._run_app_step` 中 leg 失败(rc≠0 / 需 bind 却无 reply / 输出无关断言失败 / leg judge 判 fail)一律 raise,整条 flow 终止。

### R0 失败分类税(第 1–2 天)

恢复策略取决于失败原因。在 hard signals 与 leg judge 的汇合点产出结构化 `failure_kind`:

- `env_fail` — 设备/IME/adb 层 → **不重试**,终止并报设备问题;
- `route_fail` — judge 判"答非所问/进错功能" → 先换措辞重试一次(比换路由便宜),再换路由;
- `app_fail` — 入口正确但 App 端未完成(风控墙/超时/崩溃)→ 从重试档开始;
- `judge_uncertain` — 有 reply 但置信度低 → 只重试一次,不升级。

落点:`leg_verdict.json` + `summary.json` 各加一个字段;leg judge 的 prompt 加分类输出,不增加调用次数。

### R1 重试档(第 3–5 天)

同 app 同 capability 原地重来:force-stop(复用 pre-kill)→ fresh conversation → 若 `route_fail/judge_uncertain`,先用一次廉价 LLM 调用换措辞(输入 = 原 prompt + judge 失败理由 + 截断的错误回复)。**`prompt_template` 卡片只允许重抽槽位、不允许改模板措辞**(措辞固化是该机制的设计初衷)。

**预算护栏(P1 全局)**:每 leg ≤1 次重试、每 flow ≤2 条恢复 leg、恢复新增 token 上限(默认 15k);`RELAY_RECOVERY=0` 整体关闭、回到现行为(benchmark 可比性依赖这个开关)。

**安全红线(与 R1 同时就位)**:`handoff_to_user_required: true` 的 capability,重试前必须确认上次运行未越过 handoff 点(检查 traj 末 action);拿不准就不重试,直接出部分成功报告。恢复机制不得制造双份下单。

### R2 换路由档(第 6–9 天)

三段式 router 增加 `exclude: [(app_id, capability_id)]`(stage-1 prefilter 与 stage-2 rerank 各挡一道),以失败对为排除项重路由该 leg;无次优卡则升级 R3。失败结果同时喂给 route overlay 作负信号(它本就消费 leg verdict)。

### R3 局部 replan + 运行期 MW 兜底(第 10–13 天)

- `FlowPlanner.replan_tail(plan, failed_step, blackboard, failure_summary)`:只重排失败点之后的步骤,已完成 leg 的 bind 产出作为既成事实注入合成 prompt;
- 把 plan 期「unsatisfiable → MW leg」的转换开放到运行期:R2 也失败的 leg 转 `type: mobileworld` 执行,answer 回灌 blackboard(机器已在 `flow_planner_mw`);
- **终点站**:梯子用尽不再 raise,产出部分成功报告——哪些 leg 成了、blackboard 已有什么、卡在哪、建议用户如何接手。

### R4 遥测 + 评测(第 14–18 天)

- 每次恢复尝试落 `recovery.json`(档位/原因/成本/结果);`plan_summary` 增加 first-try success 与 final success 两列;
- phaseB 三基准各抽 ~30 条失败任务重跑(`--ids-file`),报告成功率提升、各档命中率与 token 通胀;**命中率 <10% 的档位砍掉**;
- 无设备单测:给 `InProcessLegExecutor` 加故障注入包装(第 n 次调用返回指定 `failure_kind`),锁升级逻辑与预算护栏。

---

## P2 流式抓帧 + 延迟工程(~2 周,与 P1 并行;P1 在 flow 层、P2 在 device 层,互不重叠)

**现状**:实测 screencap ~1.2s/帧是单步最大成本;step_wait 0.5s / blind-step 0.15s / poll-skip 0.3s 等固定 sleep 的存在原因正是"帧太贵,只能猜动画时长"。

### S1 scrcpy 流式后端(第 1–4 天)

- 新增 `agents/device/android_stream.py`:push scrcpy-server → `app_process` 起服务 → adb forward 取 H.264 流 → PyAV 解码,常驻线程维护最新帧缓冲,`screencap()` 变读缓冲(毫秒级);
- 挂在现有后端接缝:`RELAY_CAPTURE_BACKEND=screencap|scrcpy`(默认不变),启动失败 **warning 级**记录后自动回落 screencap(仓库惯例:fallback 必须响);scrcpy-server 二进制不入库(首次使用下载校验或文档指路);
- 端侧 App 不受影响(它走 MediaProjection 自有抓帧),这是纯主机侧优化。

### S2 固定 sleep → 帧差稳定检测(第 5–8 天)

帧便宜后,`wait_until_stable(timeout, epsilon)`(连续两帧 hash 差低于阈值即返回)逐个替换固定 sleep,每换一处跑一遍冒烟。`wait_for_reply` 的 stage-1 precheck 天然受益。动画实际 0.3–0.8s、固定 sleep 取的是保险上界——这是省时间的主力。

### S3 等价性验证 + 评测(第 9–12 天)

关键风险是**行为漂移**:解码帧与 screencap 帧的色彩/压缩差异可能影响 VLM grounding 与区域 hash。同任务两后端各 n=3 对比 action 序列与成功率,必要时重标定 hash 阈值。之后 phaseB 抽 30 任务测墙钟。**诚实预期**:in-app agent 自身的回复延迟(单次 ~18s 量级)不归我们管,任务级目标是降 ~30%,不是数量级。

---

## P3 用户记忆层(~2 周)

**原则**:本地、明示、可查删。项目卖点是"上下文已在 App 里",记忆层只补用户没说全的偏好,不做爬取。

- **M1 画像存储**(2 天):`~/.relayagent/profile.yaml`(Android 用 filesDir,复用 `RELAY_TRAJ_ROOT` 式重定向):地址簿、饮食偏好、常用联系人别名、每 App 提示。schema 入 `spec/` 并校验。
- **M2 注入点**(4 天):① `FlowPlanner` 合成 prompt 附画像摘要("寄到家"直接解析,少一轮 ask_user);② `prompt_template` 槽位抽取时画像作候选来源;③ `ask_user` 的 `select_from` 预选上次选择。
- **M3 记忆写入**(3 天):flow 成功后一次廉价 LLM 调用判断"是否出现值得记的稳定偏好",**问一句才写**(y/n),绝不静默记。
- **M4 隐私配套**(2 天):画像值会随 prompt 进 traj 日志 → `RELAY_TRAJ_REDACT=1` 落盘前替换为 `<profile:home_address>` 式占位。没有这个,用户分享 traj 排障即泄露地址。
- **验收**:10 条含隐式偏好的自造任务(寄到家/老样子/上次那家),对比有无画像的 ask_user 轮数与成功率;泄露扫描验证 traj 无画像明文。

---

## P4 卡片回归 CI + 半自动生成

- **C1 卡片健康 CI**(1 周,先做——保护存量资产):夜间逐卡:安装态检查(复用 `check_device_env`)→ 走 entry path 断言可达(`native_runner --max-step` 限步,不发 prompt,零 token);每周全量:逐 capability 发一条 example_prompt 断言 reply 捕获。产出健康表;连续两晚失败自动开 issue(`card_issue` 模板 + `gh` CLI)。硬件起步 = 一台真机 + relay-test AVD(国际 App)。
- **C2 卡片录制器**(3 周):`scripts/card_recorder.py`——人在真机上手走一遍进入 in-app agent 的路径,录制器旁听 a11y 事件流,把点击序列翻成 entry path 选择器草稿,发探测 prompt 归类 capability,吐出带 `provenance` 骨架的 YAML → 人工修订 → PR。把建卡成本从一天压到一小时,是社区贡献起量的关键杠杆。
- **C3 全自动发现**(不排期):探索式 agent 自寻 AI 入口,等 C2 积累"入口长什么样"的数据后再议。

---

## P5 多平台 / OEM(长线并行,里程碑制)

- **H1**:HarmonyOS App(`harmony/`)与 `android/` 功能对齐,hdc backend 从骨架变可用;
- **H2**:iOS WDA backend 跑通一张国际 App 卡(Booking / Copilot 有 iOS 版,`app_ids` 多平台映射机制现成);
- **H3**:OEM 对话——弹药是技术报告的 11–19× token 数字与 SPEC §14 的 A2A 前向兼容叙事("现在用卡片,你们发 endpoint 后卡片退化成 shim")。**P1 做完再谈**:42–67% 的成功率进不了 OEM 的门,80%+ 可以。

---

## 执行顺序与全局纪律

```
周 1-3   P1 恢复闭环(R0→R4)
周 1-2   P2 流式抓帧(并行,S1→S3)
周 4     P1+P2 合并后 phaseB 全量重跑一轮(数字同时是 OEM/论文弹药)
周 5-6   P3 记忆层
周 5     P4-C1 卡片 CI(小,插空)
周 7-9   P4-C2 录制器
持续     P5 里程碑推进
```

两条全局纪律:

1. **每阶段收尾重跑 phaseB 子集**,数字进 `report/`——项目信誉建立在"每个 claim 有 n=3 数据"上,产品化阶段不丢;
2. **所有新行为都有 env 开关且默认与今天一致**(`RELAY_RECOVERY` / `RELAY_CAPTURE_BACKEND` / `RELAY_PROFILE` / `RELAY_TRAJ_REDACT`),任何时候都能退回可比基线。
