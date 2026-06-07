# 自动跨 App 规划器（`run_plan.py` / `FlowPlanner`）

> English: [`cross_app_planner.md`](cross_app_planner.md)

> 一句自然语言 → LLM **自动合成**一条跨 App 的 plan → 校验 → 落盘 → 预览确认 → 真机执行。
>
> 与已有入口的关系：`run_plan.py` 现在是自然语言入口；指定 App 的直跑用 `python -m agents.native_runner`。

---

## 1. 它解决什么

原来的单 App NL router 只能把一句话路由到**单个** app + capability，无法把跨 App 目标拆解成多 app 步骤序列。给一句横跨多个 app 的指令时，它会回落到单 app 或选错。

`run_plan.py` 补上的就是这一层：**给定全量 app/capability 清单，让 LLM 动态产出 steps + bind 关系**，再交给 `FlowRunner` 执行。**不重造执行器，只新增"规划"这一层。**

---

## 2. 流水线

```
一句话
  │
  ├─(1) build_catalog()            全量 app + capability（agents/card_catalog.py）
  │
  ├─(2) 缓存查找                    manifests/_generated/ 里精确串匹配；--no-cache 跳过
  │        命中 ┐
  │            └────────────────────────────────┐
  ├─(3) FlowPlanner.plan()         未命中：LLM 合成 → fenced JSON → 本地校验
  │        │                                     │
  │        ├─ 校验不过 → 硬报错退出（repair 是 TODO）
  │        └─ unsatisfiable → 打印原因退出
  │                                              │
  ├─(4) 落盘                        写 plan YAML 到 manifests/_generated/
  │                                              │
  ├─(5) 预览 + 确认 ←───────────────────────────┘
  │        默认 N；非交互 stdin（EOF）= 不执行；--yes 跳过；--dry-run 到此为止
  │
  └─(6) FlowRunner.run()           每个 app leg 一个 native runner 子进程（直 adb）
```

涉及的文件：

| 文件 | 角色 |
| --- | --- |
| [`scripts/run_plan.py`](../scripts/run_plan.py) | CLI 入口：缓存 / 落盘 / 预览 / 确认 / 录屏 / 派发 |
| [`agents/flow_planner.py`](../agents/flow_planner.py) | `FlowPlanner`：catalog → prompt → LLM → JSON → 校验（含 repair TODO 空壳） |
| [`agents/flow_runner.py`](../agents/flow_runner.py) | 执行器：跑每个 leg、bind 回复、处理 ask_user / extract |
| [`manifests/_generated/`](../manifests/_generated/) | 生成物 + 缓存目录，`.gitignore` 把内容排除出版本库 |

---

## 3. 生成的 plan schema

输出是一条 flow plan，直接喂给 `FlowRunner`。**没有 `inputs` 块**——句子是具体的，字面值直接烤进 step 的 `prompt`；leg 间数据流用 `extract` / `bind` / `{var}`。

落盘后的字段顺序（`run_plan.py:_persist` 固定）：

```yaml
flow_id: gen_<8位hash>            # 自动派生
source_request: <归一化后的原始句子>   # 精确串匹配缓存用
description: <一句话概述>
apps_required:                    # 仅供校验 / 预览展示
  - {app_id: ..., use_capability: ...}
steps: [ ... ]
```

`steps` 里每一步是以下两种之一：

**App step**（驱动一个 app 的 agent 跑一个 capability）

```yaml
- id: find_bookstores
  app: com.xingin.xhs               # 必须是 catalog 里的 app_id
  capability: qa_community_knowledge # 必须是该 app 上存在的 capability id
  prompt: "在上海找三家评价好的小众书店，列出店名、地址和简短推荐理由"
  extract:                          # 可选：仅当后续 step 要消费这步回复的结构化数据
    prompt: "抽成 JSON 数组 [{name, address}]"
    bind_to_array_key: bookstores   # 从 extract 出的 JSON 里取这个 key（数组/字符串都用它）
  bind: bookstore_list              # 可选：把这步结果存成变量，下游用 {bookstore_list} 引
```

**Ask-user step**（把控制交给用户，再继续）

```yaml
- id: pick_bookstore
  type: ask_user
  bind: selected_bookstore
  prompt_header: "请从以下推荐的小众书店中选择一家："
  select_from: bookstore_list       # 可选：渲染成编号选单从该列表选 1
  item_label: "{name}（{address}）"   # 可选：每个列表项怎么显示
```

模板：`{var}` 和 `{var.field}` 对 blackboard 取值（FlowRunner 的 `render()`）。blackboard 起始为空（无 inputs），随每步 `bind` 增长。

---

## 4. 规划器规则（写进 system prompt）

`FlowPlanner._PLANNER_SYSTEM` 给模型的硬约束：

1. **只能用 catalog 里出现的 `app_id` / capability id，绝不编造。**
2. **跨 step 传数据**必须给上游 app step 配 `extract` + `bind`，下游用 `{var}` / `{var.field}` 引；**引用的每个 `{var}` 必须由更早的 step 产出。**
3. 某步回复是**列表**且要用户选时，插一个带 `select_from` 的 `ask_user`。
4. **`handoff_to_user_required` 的 capability：**
   - 若它**不是**整个任务的最后一步 → **必须**紧跟一个 `ask_user`（用 `prompt_header` 显示 agent 抛出的话、收用户回答），再接一个消费该回答的 app step（在那步 prompt 里**重述完整意图**，因为它是全新 agent 会话）。
   - 若它**是**最后一步（如末尾打车）→ **可以**作终点：它自己的 in-app handoff 就是用户的终态确认，无需再补 `ask_user`。
5. prompt 尽量用用户原话，只补明显缺口；把请求里的具体值直接烤进 prompt。
6. 单 app 请求也行——出 1-step plan。
7. 现有 app 组合**无法满足**时，返回 `{"unsatisfiable": true, "reason": "..."}`，预览阶段如实告知、不执行。

---

## 5. 本地校验

`FlowPlanner._validate()` 在执行前挡住坏 plan（返回错误清单，空 = 通过）：

- `steps` 是非空 list；
- 每个 app step：`app` / `capability` / `prompt` 齐全，`app` 命中 catalog，`capability` 命中该 app；
- prompt / `extract.prompt` / `prompt_header` 里引用的每个 `{root_var}` 都已被更早的 step bind（**挡悬空引用**）；
- `ask_user` 的 `select_from` 指向已 bind 的变量；
- `bind` 名唯一、`id` 不重复；
- **规则 4 的校验**：`handoff_to_user_required` 的 capability 若**不是**最后一步，下一步必须是 `ask_user`（末尾的允许作终点）。

> **校验失败 = 硬报错退出**，打印错误清单 + 原始 plan。**LLM repair 重修循环是 TODO**（`FlowPlanner._repair` 是空壳），刻意不实现，以免坏 plan 静默执行。

---

## 6. handoff 往返：先 A 后 B

"`handoff_to_user` 后要能 switch 回 agent" 分两个粒度，本版落地 A、给 B 留缝：

- **Phase A（已落地）**：handoff leg 结束 → 流程级 `ask_user` 收用户回答 → 下一个 agent leg 是一个**全新 native runner 子进程**（同 app 或换 app），把回答 + 完整意图重述进 prompt。复用现有 FlowRunner 结构。**局限**：同 app 中途 handoff 会冷启动、清历史，丢 in-app 半成品状态。
- **Phase B（仅留缝，未接线）**：让 handoff leg **不终止**——把 `flow_runner._run_app_step` 里的 `stdin=DEVNULL` 换成 flow⇄agent 回环通道；agent 的 handoff `ask_user`（`relay_agent.py` 内）不再 EOF 收尾，而是阻塞读 flow 喂回的答案再继续 `predict()`，在**同一会话**里原地续跑。两处都打了 `# TODO(phase-B):` 标记。

> in-app handoff 现状：agent 走到 `handoff` 步时先 `_maybe_persist_reply()`（把回复写 `RELAY_REPLY_OUT`），再发 `action_type="ask_user"`；flow leg 里 stdin 是 DEVNULL → 立刻 EOF → 子进程结束、回复已落盘。

---

## 7. 缓存

- **落盘**：校验通过的 plan 写 `manifests/_generated/<slug>_<hash8>.yaml`，内含 `source_request`（归一化后的原句）。
- **复用**：规划前扫 `_generated/`，**`source_request` 归一化后精确相等**就直接复用那份（仍走预览 + 确认，不再调 LLM）。`--no-cache` 跳过。
- **语义复用是 TODO**：精确串没命中时回落到 embedding / LLM 判相似——代码里 `run_plan.py:_cache_lookup` 留了 `# TODO(semantic-cache):` 钩子，暂未实现。

---

## 8. 用法

```bash
# 基本：合成 → 预览 → 询问 y/N → 执行
uv run python scripts/run_plan.py "在上海找三家评价好的小众书店，挑一家打车过去"

# 只规划 + 预览，不执行（不碰设备、只调一次 LLM）
uv run python scripts/run_plan.py "..." --dry-run

# 跳过确认直接执行（自动化 / 真机批跑）
uv run python scripts/run_plan.py "..." --yes

# 忽略缓存，强制重新生成
uv run python scripts/run_plan.py "..." --no-cache

# 录屏（parent-owned，跨 leg 连续录；自动开 RELAY_SKIP_STEP_SCREENSHOT）
uv run python scripts/run_plan.py "..." --record
uv run python scripts/run_plan.py "..." --record /path/to/dir

# `--` 之后的参数透传给底层每个 native runner
uv run python scripts/run_plan.py "..." -- --step_wait_time 0.3
```

**flag 一览**

| flag | 作用 |
| --- | --- |
| `--dry-run` | 合成 + 预览后停，不执行 |
| `--yes` / `-y` | 跳过 y/N 确认 |
| `--no-cache` | 不复用 `_generated/` 里的缓存，强制重新生成 |
| `--record [DIR]` | adb 录屏；缺省落 `traj_logs/recordings/<ts>/` |
| `-- <args>` | `--` 之后透传给底层 native runner |

**环境**

- 规划用 `.env` 的 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`（=`qwen`）。
- 执行时每个 leg 是一个直 adb 的 native runner 子进程。
- 真机要求同项目其余部分：adb + USB 调试 + `com.android.adbkeyboard/.AdbIME`，`RELAY_ANDROID_SERIAL` 选设备。

**中途 `ask_user` 的非交互行为**：流程级 `ask_user` 读父进程 stdin。`< /dev/null`（或管道 EOF）时，选单步**自动取第一个候选**、自由输入步取空串——适合 `--yes` 批跑。要人工选就在真终端里交互运行。

**安全**：默认 N；非交互 stdin 视为不执行。带 `handoff_to_user_required` 的 capability（如打车、下单）会停在**不可逆 CTA 之前**交还，不会真下单/付款。

---

## 9. 已知局限 / TODO

| 项 | 状态 | 位置 |
| --- | --- | --- |
| LLM repair 重修循环 | TODO（硬报错代替） | `flow_planner.py:_repair` + `# TODO(repair):` |
| 语义缓存复用 | TODO（仅精确串匹配） | `run_plan.py:_cache_lookup` `# TODO(semantic-cache):` |
| Phase B 同会话续跑 | 仅留缝 | `flow_runner.py` / `relay_agent.py` 的 `# TODO(phase-B):` |
| 静态一次性规划 | 设计如此 | 不做逐步 / 失败重规划；leg 输出异常不自适应 |
| plan 复杂度 | 不设硬上限 | ≥4 个 app leg 时只 `logger.warning` 提示 |

---

## 10. 一次真机实跑（worked example）

输入：`"在上海找三家评价好的小众书店，挑一家打车过去"`（Pixel 9，`--yes`，stdin `</dev/null`）。

合成的 plan：

```
1. [agent]    小红书 / qa_community_knowledge  →  extract → bind bookstore_list
2. [ask_user] 从 bookstore_list 选 1           →  bind selected_bookstore
3. [agent]    高德地图 / hail_ride（末尾 handoff，作终点）
```

执行轨迹：

- **Leg 1（点点 qa）**：点点回复 915 字 → extract 抽出 3 家 `[{犀牛书店,…},{i人书房,…},{1691 Coffee Bar,…}]` → bind `bookstore_list`。task wall_s **79.1s**。
- **ask_user**：stdin EOF → 自动取第一家 **犀牛书店** → bind `selected_bookstore`。
- **Leg 2（高德 hail_ride）**：prompt 渲染成 `帮我叫一辆车去 犀牛书店，地址是 苏州河畔老建筑`（`{selected_bookstore.name}` / `.address` 跨 leg 传值生效）→ agent 走到 handoff、**停在打车确认前交还，未下单**。task wall_s **68.6s**。

整条跨 app 任务 ≈ **2.5 分钟**，全程 exit 0、无 error。

---

## 11. 改动说明（本次引入）

新增"自动合成跨 App plan"这一层，全部改动如下。

**新增**

| 文件 | 内容 |
| --- | --- |
| `agents/flow_planner.py` | `FlowPlanner`：catalog → system prompt → LLM → fenced JSON → 本地校验（`_validate`）。`PlanValidationError` 携带错误清单。`_repair` 是 TODO 空壳。 |
| `scripts/run_plan.py` | CLI 入口：精确串缓存 / 合成 / 落盘 / 预览 / 确认 / 录屏 / 派发 `FlowRunner`。flag：`--dry-run` `--yes` `--no-cache` `--record` `-- <透传>`。 |
| `manifests/_generated/.gitignore` | 把生成的 plan / 缓存排除出版本库，只保留 `.gitignore` 自身。 |
| `docs/cross_app_planner.zh.md` | 本文档。 |

**修改**

| 文件 | 改动 |
| --- | --- |
| `agents/flow_runner.py` | ① `# TODO(phase-B):` 缝注释（`stdin=DEVNULL` 处）。② `_traj_stem()`：用 plan 涉及的 app 命名 traj 目录——`plan_<app1>_<app2>…`——而非冗长的 NL-slug 文件名。 |
| `agents/relay_agent.py` | handoff 分支加 `# TODO(phase-B):` 缝注释（不改逻辑）。 |
| `CLAUDE.md` | `跑测试` 加 run_plan 入口；新增"自动跨 App 规划"速览节并指向本文档；"三个→四个入口脚本"。 |
| `README_zh.md` | scripts 目录清单加 run_plan；`自然语言入口` 后加"自动合成跨 App plan"小节。 |

**设计决策记录（为什么这么做）**

- **静态一次性规划**而非逐步/ReAct：复用现有 `FlowRunner`，改动最小、可立即落地；代价是 leg 输出异常不自适应。
- **独立 `run_plan.py`** 作为 NL flow 入口：单 App 请求变成 1-step plan，多 App 请求也走同一条执行路径。
- **预览 + 确认（默认 N）**：跨 App 含不可逆副作用（打车/下单），执行前必须人能看清并放行。
- **校验失败硬报错、repair 留 TODO**：宁可中止也不让坏 plan 静默执行。
- **handoff 末尾可作终点、中间才强插 ask_user**：如一条收尾在打车的 plan；末尾 leg 的 in-app handoff 本身即终态确认。
- **缓存先精确串匹配、语义复用留 TODO**：先把主链路跑通，避免在缓存上过早投入。
