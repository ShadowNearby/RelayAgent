# NL 跨 App Flow 架构

> English: [`nl_flow.md`](nl_flow.md)

> 一句自然语言 → 自动合成的多 App 协作计划 → 执行。本文是**架构深挖**（合成 / 三段式路由 / 校验 / 执行 / leg judge / handoff / 路由固化）。
> pipeline、CLI 用法、缓存、真机实跑示例见 [`cross_app_planner.zh.md`](cross_app_planner.zh.md)。
>
> 涉及代码：`agents/flow/flow_planner.py` / `agents/flow/flow_runner.py` / `agents/routing/capability_matrix_router.py` / `agents/flow/leg_judge.py` / `agents/routing/route_overlay.py` / `scripts/run_plan.py`。

---

## 1. 总览

```
NL request
  └─ FlowPlanner.plan()            合成 plan（一次 LLM，static one-shot，不做分步重规划）
       ├─ resolve_app_routes()     每个 app step 用三段式 router 定 app+capability
       │    └─ _fill_prompt_template / _maybe_localize_prompt  填 submit prompt
       ├─ 校验 + repair 回路        本地校验；命中错误就把错误清单喂回 LLM 重修（≤3 轮）→ 重路由 → 重校验
       └─ 落盘 manifests/_generated/*.yaml
  └─ FlowRunner.run()              按 step 顺序执行
       ├─ app_step    → spawn `python -m agents.runtime.native_runner` 子进程（一 leg = 一 app + 一 capability）
       ├─ ask_user    → 终端收人类输入（可渲染 select_from 选择列表）
       └─ extract     → 文本 LLM 把上一 leg 回复解析成结构化 JSON
       blackboard：{var}/{var.field} 在 step 间传值
```

关键设计取舍：

- **Static one-shot planning**：planner 一次吐出整张计划，**不**在这里做分步重规划。
- **plan 复用 flow yaml 形态**（`app_step` / `ask_user` / `extract` / `bind`），所以**不需要新 executor**，`FlowRunner` 直接执行。没有单独的 `inputs` 块——请求是具体的，字面值直接 bake 进 step prompt。
- **每个 app step 是全新子进程**，不复用一个长生命周期 RelayAgent 跨 App：plan cursor / chat history 都是 single-card 作用域的。
- **router 在子进程里被旁路**：`RELAY_FORCE_CAPABILITY` + `RELAY_INVOCATION_TEXT` 让子进程跳过路由 LLM 调用，直接进 plan building。

## 2. Plan 合成（`FlowPlanner.plan`，`_PLANNER_SYSTEM`）

输入：`build_catalog()` 产出的全 App catalog（含每个 capability 的 id / description / example_prompts / executable / `handoff_to_user_required` / `x_skip_wait_for_reply`） + 用户 NL。

输出 plan（`json` fence，`temperature=0`）：

```json
{
  "description": "<一句话计划摘要>",
  "apps_required": [{"app_id": "...", "use_capability": "..."}],
  "steps": [ <step>, ... ]
}
```

**step 两类：**

- **App step**：`{id, app?, capability?, prompt, extract?, bind?}`
  - `app` / `capability` 是 **provisional**（暂定）：planner 只管拆任务 + 写每步具体 prompt，最终 app/capability 由后面的 matrix router 定。
  - `extract`（可选，仅当下游要消费结构化数据）：`{prompt, bind_to_array_key}`。
  - `bind`（可选，下游不需要就省）。
- **Ask-user step**：`{id, type:"ask_user", bind, prompt_header, select_from?, item_label?}`——把控制交回人类再继续；`select_from` 渲染编号选择列表。

**planner 的硬规则**（system prompt 内）：

1. 只用 catalog 里出现的 id，绝不发明。
2. step 间传值靠上游 `extract`+`bind`，下游 `{var}`/`{var.field}` 引用；**每个 `{var}` 必须由更早的 step bind 产生**。
3. 回复是「用户该从中挑一个的列表」→ 插 `ask_user` + `select_from`。
4. **`handoff_to_user_required` capability 永不作为非终态的最后动作**：若它不是整个任务的最后一步，**必须**后接 `ask_user`（展示 agent surfaced reply → 收用户答案 → 再接一个消费答案的 app step，且**重述完整意图**因为它跑全新 agent 会话）。若它**就是**最后一步（如最后打车），可作终态——它自己的 in-app handoff 就是用户最终确认。`x_skip_wait_for_reply` 的 step 不抓文本回复，别给它 `bind`/`extract`。
5. prompt 优先用用户原话；每个 app-step prompt 默认用该 App 第一个 `locale` 语言（用户显式要别的语言才换）；专名/地址/产品名/代码/URL/邮箱/id/引用原文保留原语言。
6. 单 App 请求 OK——一步计划即可。
7. **`foundation_llm` 是通用信息/知识兜底**：信息 / 问答 / 总结 / 起草 / 查询类任务，若无专属 capability 覆盖（解释 GitHub repo、总结 arXiv 论文、通用知识），路由到 `foundation_llm` capability，而不是判 unsatisfiable。
8. 仅当任务**要求一个现有 capability 给不了的具体设备/App 动作**（往 catalog 没有的聊天平台发消息、拍照）**且 `foundation_llm` 顶替不了**时，才返回 `{"unsatisfiable": true, "reason": "..."}`。

## 3. 三段式路由（`capability_matrix_router.route`）

单 App NL 路由策略的可复用版。`docs/app_capability_matrix.csv` 是 **cap × app 归属的 source of truth**；catalog 仅作 availability 校验（剔除 matrix 里指向已不存在 (app,cap) 的 stale 项）。

- **Stage 0 — 固化短路**（route overlay，见 §9）：当传入 `route_key`+`overlay` 时，三段式**之前**先查固化表——命中且 `(app,cap)` 仍在 catalog → 直接返回，**0 次 LLM**。冷表/低置信回落下面三段，行为不变。
- **Stage 1 — 垂类预筛**（`_stage1_prefilter`）：给 LLM 一份**不含 `foundation_llm`** 的垂类 capability 菜单（id+desc），选 ≤3 个最可能命中的 cap id。垂类都不合适就返回空 list。
- **Stage 2 — rerank**（`_stage2_rerank`）：把 stage-1 命中的 cap × matrix 授权的可运行 App 展开成 (app, capability) 选项（带 desc / 例子 / locale），LLM 选**唯一**最佳并写 goal 句子。
  - **早退**：选项只剩 1 个直接返回，不调 LLM。
  - LLM 返回 `kind:"none"` 或选了 shortlist 外的 pair → 回落 stage-3。
  - 命中垂类 cap 但 matrix 授权 0 个可运行 pair → 抛错（不静默吞）。
- **Stage 3 — foundation 兜底**（`_stage3_foundation`）：垂类都不 fit 才进。**这是结构上独立的一段，不是 prompt 里一句提示**——在 foundation_llm App 里选最佳通用助手并写 goal。
  - **逃生口（不是无条件 catch-all）**：foundation 助手只产出**文本答案**；若任务要求一个聊天助手做不到的**具体设备/OS 副作用动作**（文件管理、重命名/移动/删除文件、改系统设置、驱动别的 App UI），stage-3 返回 `kind:"none"` → 抛 `FoundationNotApplicable`。`_route_one_step` 把它**当 coverage gap** 处理（打 `x_coverage_gap`、进 gaps），让 repair 重试，仍未闭合则 `_apply_mw_fallback_to_gaps` 转 **MobileWorld leg**，而不是硬塞进 foundation_llm。例：「把 Download 里 `bid_` 前缀文件按创建日期重命名」。

`route(..., preserve_goal=True)`：flow 规划只用 router 选 app/capability，**保留 planner 自己的 templated prompt**（把 `goal` 强制设回原 NL，不让 router 改写措辞）。

> **locale policy**：goal 句子默认用所选 App 第一 locale 语言；用户显式要别的语言才换；专名/地址等保留原文。三段都带这条。

## 4. 路由后填 prompt（`_route_one_step`）

入口先算并盖上 `step["x_route_key"]`（归一化 prompt 的 sha1，供 §9 固化回路；复用已持久化的 key 避免 cache 重跑漂移），并把 `route_key`+`overlay` 传给 router。只有路由完知道了 capability，才知道用哪个模板，所以填 prompt 在这步：

- capability 有 `prompt_template` → `_fill_prompt_template()`（抽槽位填固定模板），**跳过** `_maybe_localize_prompt`（模板本就按 App locale 写）。任何失败硬错。
- 否则 → `_maybe_localize_prompt()`：原 free-synthesis prompt 若与 App locale 不兼容且用户没显式指定语言，调一次 LLM 改写成目标语言。改写**必须保持 `{var}` 占位符集合不变**，否则丢弃改写保留原文。

完整 prompt 模板机制见 [`prompt_template.zh.md`](prompt_template.zh.md)。

## 5. 本地校验 + LLM repair（`_validate` → `_repair`）

合成完（含路由）跑本地校验。校验内容：

- step 必须是 object；`id` 唯一；`bind` 名唯一。
- app step：`app`/`capability`/`prompt` 非空；`app` 在 catalog 内；`capability` 属于该 app。
- prompt / `extract.prompt` 里每个 `{var}` 必须**已被更早 step 产生**。
- **Rule 4**：mid-flow 的 `handoff_to_user_required` leg 必须后接 `ask_user`；作为**最后一步**的 handoff leg 是合法终态。
- `x_skip_wait_for_reply` 的 cap 不能带 `bind`/`extract`。
- ask_user：必须有 `bind`；`select_from` 必须是**字符串**且由更早 step bound（归一到**根名**比对，故 `{var}` / `var.field` 形式都接受；非字符串记成校验错误而非崩）；`prompt_header` 的 `{var}` 必须已 bound。

**Repair 回路**（`plan()` → `_repair`）：命中路由**或**校验错误时，把坏 plan + 错误清单喂回 LLM 要一份修正 plan（同 schema），再重路由 + 重校验——最多 `_REPAIR_ROUNDS`（3）轮。轮次用尽才由 `plan()` 抛 `PlanValidationError`（带完整 error list）——且仅当 MW 兜底**关**时才抛；开着时改为整条转 MW leg，不抛（见 §10）。repair 轮也可能合法返回 `{"unsatisfiable": ...}`。（`validate_plan()` 单独作用于**缓存** plan 时仍直接硬失败、不 repair——缓存 plan 落盘时已校验过。）模型返回的坏 JSON（字符串里裹裸控制符）由 `_parse_fenced_json` 的 `json.loads(strict=False)` 容忍。

路由阶段还会清理：`_drop_unused_no_reply_binds`（no-reply step 上下游没人引用的装饰性 `bind`/`extract` 去掉）、`_refresh_apps_required`（按实际路由结果重建 `apps_required`）。

## 6. 执行（`FlowRunner.run`）

按 step 顺序跑，blackboard `self.bb` 起空、随 step bind 增长。`render()` 做 `{var}`/`{var.field}` 替换（缺键 → `''`）。

第一个 step 之前，`nl_flow.execute_plan` 会把 plan 涉及的所有 App 先 force-stop 一遍（best-effort，kill 失败不阻塞运行），保证没有 leg 接到后台残留的旧会话。`RELAY_PREKILL_APPS=0` 关。

**App step（`_run_app_step`）：**

- 每个 leg 一个全新 `python -m agents.runtime.native_runner <app> <prompt>` 子进程。
- 子进程 env：`RELAY_FORCE_CAPABILITY`/`RELAY_INVOCATION_TEXT`（旁路 router）、`RELAY_SKIP_OPEN_APP=1`+`RELAY_AGENT_LAUNCH=1`（deferred-launch：冷启动放 agent 首帧 predict，把进程/leg 启动开销排除在 leg 墙钟外）、`RELAY_TRAJ_DIR`（把 traj.json / steps/ / agent_reply.json 直写进本 leg 的 `NN_<id>/` 目录——无全局 `user_task` scratch、无跑后 copytree）、`RELAY_REPLY_OUT`（回复 JSON）、`RELAY_SUMMARY_OUT`（summary）、`RELAY_WALL_OUT`（agent 写 framework-excluded `wall_clock.json`）。
- stdin 喂 `DEVNULL`：末尾 ask_user handoff 以 EOF 干净收尾，不阻塞 flow。
- **每 leg traj 单独存**：每个 flow run 有自己的 traj root `traj_logs/<ts>_plan_<app1>_<app2>.../`，每 leg 一个 `NN_<id>/`。子进程经 `RELAY_TRAJ_DIR` 直接把轨迹写进该 leg 目录；native runner 被 pin 时跳过全局 backup 轮转。详见 [`trajectory_logging.zh.md`](trajectory_logging.zh.md)。
- **回复 / 硬信号**：从 `RELAY_REPLY_OUT` 读 reply。需要 reply（有 `bind`/`extract`）却没拿到 → 抛错。no-reply leg 走 `_assert_output_free_step_completed`：必须 `rc==0` 且 last_action ∈ {ask_user, answer} 或 (finished 且 goal complete)，否则抛错。
- **Leg judge**（语义层，见 §7）：硬信号之上的「自信地答错」检测。

**Ask-user step（`_run_ask_user`）：** `select_from` 渲染编号列表（`item_label` 模板控制每项显示），用户输入经 `_resolve_choice`（编号 / 子串匹配 label 或 name / 空选第一项）；或纯 freeform。EOF → 空。

**Extract（`_extract`）：** 对 `.env` 同端点跑文本-only chat completion，把上一 leg 回复解析成 fenced JSON；`bind_to_array_key` 从结果对象里拎出某 key。

## 7. Leg Judge（`leg_judge.py`，语义结果判定）

**leg** = 一个 native-runner 子运行，钉死一 app + 一 capability。`flow_runner` 的硬信号（崩溃 / 空回复 / 非终态）只抓**显性**失败，区分不了「自信地答错」和「答对」。

- 镜像 MobileWorld `BaseTask.is_successful` 契约（`-> (score, reason)`，1.0 成功 / 0.0 失败），但开放世界无 per-task ground-truth oracle，**改为让 VLM 读** leg 的 goal + 抓到的回复 + 最后屏幕，三态分类：**`loading`**（仍在进行，结果未定，**不算失败**）/ **`success`** / **`failure`**。
- **取最后 n 帧**（`final_frames`，默认 2 帧，StepLogger 落的 pre-action PNG）：发最后两帧而非一帧，让 judge 能区分卡死/loading vs 已 settle 的终态。
- **handoff leg**（terminal_action == `ask_user`）改用 `_SUCCESS_HANDOFF` 定义——把最终决定交回用户是**预期不是失败**；非 handoff 用 `_SUCCESS_OUTCOME`（动作真做了 / 信息真显示）。
- **loading-retry**（`flow_runner._judge_leg`）：首判 `loading` 时（如 live_navigation 点 CTA 后地图还在转），等 `RELAY_LEG_JUDGE_LOADING_WAIT`(2s) 用 `screencap()` 抓**当前**帧重判，最多 `RELAY_LEG_JUDGE_LOADING_RETRIES`(3) 次，settle 即停。只有 loading 才付这个代价。
- **best-effort**：任何错误（无帧 / LLM 挂 / 无法解析）返回 `UNKNOWN`(`judged=False`)。**caller 绝不能让 judge 失败中断 flow**——按 CLAUDE.md fallback policy surface 后继续。`LegVerdict.score`（1.0/0.0/-1.0）落 `leg_verdict.json` 存到 leg 目录旁。`RELAY_LEG_JUDGE=0` 关。
- **折回固化表**：verdict 写盘后，`_judge_leg` 末尾调 `overlay.record(step["x_route_key"], ..., verdict.status)`，把这条 verdict 折进路由固化回路（§9）。这是固化表的**唯一写入点**，复用现有 verdict，不增加任何 LLM 调用。

## 8. Phase-A / Phase-B Handoff

- **Phase A（现状）**：handoff 在 flow 粒度——handoff leg 后接 flow-level `ask_user`，再起一个**全新** leg 消费答案。会丢 in-app 会话状态，所以后续 leg 必须重述完整意图。
- **Phase B（TODO，`flow_runner` 内注释）**：same-session handoff round-trip。leg 带 `resume:true` 时不用 EOF 关 stdin，保活子进程并接一条 flow⇄agent 通道（fifo/文件），让 in-app agent 的 handoff ask_user 阻塞等答案、在**同一会话**里 resume `predict()`，保住 in-app 状态。

## 9. 路由固化（route overlay，trace-guided solidification）

把 §7 的 leg verdict 从"只写日志"变成路由优化器的输入：被反复确认 `success` 的 `(意图 → app/capability)` 决策**固化成表查**，下次同意图零 LLM 命中；反复 `failure` 的自动失效、回落三段式。代码 `agents/routing/route_overlay.py`，存储 `traj_logs/route_overlay.json`（git-ignored 学习产物，**非权威**；matrix CSV 仍是 source of truth，提升回 matrix 是独立人工 review 步骤）。

**回路：**

```
合成 prompt ──compute_route_key(模式 a/b, 默认 b)──► step["x_route_key"]（随 yaml 持久化）
   │  §3 Stage-0 route 短路读                      │  §7 leg judge 写
   ▼                                               ▼
route(route_key, overlay):                  _judge_leg 末尾:
  lookup(key) 命中且 (app,cap)∈catalog          overlay.record(x_route_key, app, cap, status)
    → 直接返回, 0 LLM                              success→consec_fail 清零 / failure→consec_fail++
  否则 → 三段式 LLM                                原子写 route_overlay.json
```

**固化判定（`lookup`）**：某 `(app,cap)` 满足 `success ≥ MIN_HITS(3)` 且 `success_rate ≥ RATE(0.8)` 且 `consec_fail < MAX_FAILS(2)` 即返回（多个命中取 success 最高者）；否则 None → 回落 LLM。冷表/低置信永不短路，所以 **P0"影子期"天然内置**——`record` 一直在攒数据，`lookup` 在够格前都不生效（开/关 overlay 行为完全一致）。

**自纠**：连续 `MAX_FAILS` 次 `failure` 暂停该项固化（回落 LLM 重选）；一次 `success` 清零 `consec_fail` 即恢复。`loading`/`unknown` 记录但**中性**（不进 rate 分母、不动 consec_fail）。

**stale 守卫**：固化命中但 `(app,cap)` 已不在 catalog（manifest 改动 / 能力下线）→ 不短路，warning 后回落 live。

**route_key（`compute_route_key`，`RELAY_ROUTE_KEY_MODE`，默认 `b`）**：

- **模式 `a`（value-bearing）**：归一化（lowercase + 折空白）synthesized prompt 的 `sha1[:16]`。只复用重复 / 近似意图，与 plan 缓存（exact-string）同构。
- **模式 `b`（value-independent，默认，P3）**：`provisional_cap | provisional_app | locale_bucket` 的 `sha1`，带 `b:` 前缀。键在 planner **暂定 capability** + app 提示 + **请求语言桶**（CJK / latin 粗分）上，不含字面值——于是"导航去人民广场""导航去虹桥机场"**共享一条固化路由**（跨意图复用，即便 plan 缓存 miss 也省三段式）；而中文导航（高德）与英文导航（Gemini）因 locale 桶不同**不会被错误合并**。无暂定 capability 时**安全回落** `a`。

**key 稳定性**：`_route_one_step` 用 `step.get("x_route_key") or _compute_route_key(prompt, provisional_cap=..., provisional_app=...)`——cache 命中重跑时 `prompt` 已是填充后文本、`capability` 已是路由后的终值（非暂定），复用持久化 key 才不漂移。两种模式的 key 可在同一 store 共存（`b:` 前缀区分）。

**best-effort**：lookup / record 任何异常只 warning，坏 JSON 当空表，绝不中断 plan / flow。原子写（temp + `os.replace`）保证跑崩也是合法 JSON。

**三层缓存（由浅入深，逐层省 LLM）**：

| 层 | 命中省掉 | 触发 | 日志证据 |
| --- | --- | --- | --- |
| plan 缓存 | 合成 plan 的 LLM | NL 规范化 exact-string 命中 `manifests/_generated/*.yaml` | `cache hit → ...yaml` |
| route 固化 | 三段式 3 次 LLM | `success ≥ 3` 且 `rate ≥ 0.8` 且 `consec_fail < 2` | `route solidified -> ... (0 LLM)` |
| (基线) 设备执行 | — | 始终发生 → 喂 leg judge → overlay | leg judge + overlay recorded |

**Promote（`scripts/routes/promote_routes.py`，只读）**：把 trace 学到的高置信路由摆出来供人工决定是否折回 matrix——**绝不写 matrix**（CSV 是手维护 source of truth）。扫 overlay，按更高的门槛（`RELAY_PROMOTE_MIN_HITS` 默认 5 / `RATE` 默认 0.9）筛出 `(intent → app/cap)`，标注每条在 matrix 里**已授权**（确认学到的偏好与 matrix 一致）还是**未列**（候选加 ✓ 或忽略的 stale 项）。`--csv` 额外吐 review 行供人粘贴。纯逻辑、无设备/LLM/网络。

**开关 / 阈值**：`RELAY_ROUTE_OVERLAY`(默认1) / `RELAY_ROUTE_OVERLAY_PATH` / `RELAY_ROUTE_KEY_MODE`(默认 `b`) / `RELAY_ROUTE_SOLIDIFY_HITS`(3) / `RELAY_ROUTE_SOLIDIFY_RATE`(0.8) / `RELAY_ROUTE_MAX_FAILS`(2) / `RELAY_PROMOTE_MIN_HITS`(5) / `RELAY_PROMOTE_MIN_RATE`(0.9)。

## 10. MobileWorld 兜底（capability 不覆盖时）

RA 的路由建立在**人工维护的 manifest + capability matrix**上。当一条 leg（或整条请求）**没有任何 app/capability 覆盖**时，与其放弃，不如交给 **MobileWorld 的 `general_e2e`**——一个**无需 manifest 的通用端到端 UI agent**，能从当前屏自行开 app、导航完成任意目标（fork 已 pin 在 `pyproject.toml` 的 `mobile-world` 依赖里，装进 `.venv/.../mobile_world/`）。

**触发（planner，所有 unsatisfiable）**：

- **coverage gap**（命中 capability 但 matrix 无授权 app，`NoRunnableAppForCapability`；或 **stage-3 判设备动作非 foundation 任务**，`FoundationNotApplicable`）：`_route_one_step` 在该 step 打标 `x_coverage_gap`，**仍走完修复轮**（repair 可能把缺口重路由到真 capability，如 `foundation_llm`——优先于 MW）。修复用尽仍有缺口时，`_apply_mw_fallback_to_gaps` 把**带标记的那几条 step**就地转成 MW leg（`_to_mw_leg`），复验后返回**可满足**的 plan，而不是 `{"unsatisfiable"}`。gap 转 MW 后若复验仍失败（如下游 `{var}` 只有被丢的 capability 能 bind），也整条转 MW（`_mw_whole_request_plan`），不再返回 `{"unsatisfiable"}`。
- **LLM 判整条不可满足**（`plan()`/`_repair` 返回 `{"unsatisfiable"}`，此时**无 steps**）：退化为「整条请求 = 一条 MW leg」，`_mw_whole_request_plan` 返回单 leg plan（`prompt = 原始 NL 请求`）。
- **repair 用尽仍 invalid**（非 coverage-gap 的校验错误，如模板槽位抽不出、handoff 结构 RA 给不了）：`_REPAIR_ROUNDS` 轮用尽后，MW 兜底开着时**不再抛 `PlanValidationError`**，而是整条转 MW（`_mw_whole_request_plan`）；关掉才抛。

**MW leg 形态**（新 step type `type: mobileworld`）：保留 `id`/`prompt`/`bind`/`extract`；`app` 仅作**预启动提示**（无 capability 可路由）；带 `x_fallback_reason`。`_validate` 用 `_validate_mw_leg`：只要 `prompt` 非空 + `{var}` 引用已被上游 bound，跳过 app/capability/handoff 校验。`resolve_app_routes` 跳过 MW leg（缓存命中复跑同样跳过）。

**执行（`FlowRunner._run_mobileworld_step`）**：`FlowRunner` 为整条 flow **只起一次** MW server（`run()` 的 `finally` 里 `_ensure_mw_server` / `_teardown_mw_server`），多 MW leg 复用。每条 leg shell 出 `scripts/run_mobileworld.py`，带 `--no-start-server --server-url <flow 托管>`，以及 `--agent-type general_e2e --output <leg_dir>`（轨迹落 `<leg_dir>/user_task/traj.json`），有 `app` 提示则 `--app`，否则 `--no-prelaunch`。跑完 `_harvest_mw_traj` 取**最后一个 `answer` action 的 text** 当 leg reply→回灌 blackboard（`bind`/`extract` 与 app leg 同路径），并合成 `summary.json` + `agent_reply.json`。**leg judge** 照常跑（`final_frames` 在 `steps/` 缺失时回退读 `user_task/screenshots/*.png`）；MW leg **不进路由固化**（不是 matrix 表项，无 `x_route_key`）。flow 级 LLM call 照常折进 leg 的 `traj.json`。

**开关 / 旋钮**：`RELAY_MW_FALLBACK`(默认 `1`；`0` 或 `run_plan.py --no-mw-fallback` 关，关掉则恢复旧的 unsatisfiable 退出行为) / `RELAY_MW_SERVER_URL`(默认 `http://127.0.0.1:6800`) / `RELAY_MW_MAX_ROUND`(默认 25) / `RELAY_MW_TIMEOUT`(默认 600)。预览里 MW leg 标 `[MobileWorld fallback]`。

**留待后续**：MW leg 的 route solidification。
