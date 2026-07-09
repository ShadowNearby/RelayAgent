<h1 align="center">Trajectory 日志约定</h1>

<p align="center">
  <b>一次运行的轨迹落在哪、目录长什么样、谁来读——一个 env RELAY_TRAJ_DIR 统一所有写入方</b>
</p>

<p align="center">
  <a href="trajectory_logging.md">English</a> | <b>中文</b>
</p>

---

## 🎯 1. 一句话

**`RELAY_TRAJ_DIR` 决定这次运行的轨迹目录**（`traj.json` + `steps/` + `agent_reply.json` 都落这里）。

- **不设** → 默认 `traj_logs/user_task/`，standalone 调试用，启动时把上次的轮转到 `user_task_backup_<ts>/`。
- **设** → 子进程直接写进该目录，**不轮转、不碰全局 `user_task`、无 copytree**。NL flow 给每条 leg pin 一个 `traj_logs/<ts>_plan_<apps>/NN_<id>/`。

## ✍️ 2. 三个写入方读同一个 env

| 写入方 | 写什么 | 怎么取目录 |
| --- | --- | --- |
| `agents/runtime/native_runner.py:TRAJ_DIR` | seed 空 `traj.json` + 轮转逻辑 | `RELAY_TRAJ_DIR` 或默认 `traj_logs/user_task` |
| `agents/agent/relay_agent.py:_TRAJ_DIR` | `_append_llm_call` 写 `traj.json`、`_maybe_persist_reply` 写 `agent_reply.json` | 同上 |
| `agents/runtime/native_runtime.py:StepLogger` | `steps/`（逐步截图 + `steps.json`）| `RELAY_STEP_LOG_DIR`（显式覆写，最高优先）> `RELAY_TRAJ_DIR` > 默认 |

`wall_clock.json` / `summary.json` / 外部 `reply.json` 仍各走自己的显式 env（`RELAY_WALL_OUT` / `RELAY_SUMMARY_OUT` / `RELAY_REPLY_OUT`）——调用方一般把它们也指到同一 leg 目录。

## 🔄 3. 轮转：只在 standalone 发生

`native_runner._rotate_traj_dir`：

- **没设 `RELAY_TRAJ_DIR`**（standalone）：若 `traj_logs/user_task/` 已存在 → rename 到 `traj_logs/user_task_backup_<ts>/`（ts = 本次启动时刻），再 mkdir + seed 空 `traj.json`。`ls -td ...backup_* | head -1` 是**上一次**的内容。
- **设了**（flow/benchmark leg）：每条 leg 目录天然唯一，**跳过 backup rename**，只 mkdir + seed。

## 📁 4. 一条 NL flow leg 的目录形态

`flow_runner` 给子进程设 `RELAY_TRAJ_DIR=<flow_root>/NN_<id>`，于是：

```
traj_logs/plan_bard_20260608_231751/
  01_plan_route/
    traj.json          in-app agent 的 ["0"]["llm_calls"] + 顶层 flow_llm_calls
    steps/             step_<n>.png + step_<n>_marked.png + steps.json
    agent_reply.json   in-app agent 抓到的回复 {reply, target_app}
    wall_clock.json    {wall_s, phase:"task"}（RELAY_WALL_OUT）
    summary.json       {steps, last_action_type, last_goal_status, token_usage}
    leg_verdict.json   leg judge 判决 {status, score, reason, failure_kind}（flow 写）
    recovery.json      恢复梯子的逐档尝试日志（仅恢复触发时出现）
```

**没有 `01_plan_route/user_task/` 这一层**，flow 期间也不创建/不触碰全局 `traj_logs/user_task/`。

恢复尝试（nl_flow §6.1）落在带 `_retryN` / `_reroute` 后缀的兄弟 leg 目录（内部形态相同）；flow 根多一个 `flow_report.json`（每 step 结局 + 恢复尝试 + blackboard 键），成功和中途失败都会写。

## 📞 5. `flow_llm_calls` —— flow 进程的 LLM call 也落进 leg

in-app agent 的 LLM call 由 `relay_agent` 落在 `traj.json` 的 `["0"]["llm_calls"]`。但 **flow 进程**自己还会调 LLM：

- **leg judge**（`agents/flow/leg_judge.py`，带截图，loading 重试可能多次）
- **bind extract**（`flow_runner._extract`，纯文本抽槽）

这些走的是 flow 进程里一个**裸 OpenAI client**，本来不经过 agent 那层 instrument、也不进任何 traj。现在 `FlowRunner` 用 `_RecordingLLM` 包这个 client：每次 `chat.completions.create` 记一条（`purpose` / `model` / 脱敏后的 `messages`（base64 截图换成 `<base64 image, N chars>`）/ `usage` token / `elapsed_s` / `response`），按 leg 切片 fold 进该 leg `traj.json` 的**顶层 `flow_llm_calls`**（与 in-app 的 `["0"]["llm_calls"]` 区分）。`purpose` 由调用点打标（`leg_judge` / `bind_extract`）。

> 注意：flow **规划期**（`run_plan.py` / `FlowPlanner` 的合成、三段式路由 fallback）的 LLM call 在另一个阶段，**不**经过 `FlowRunner._RecordingLLM`，目前不落 `flow_llm_calls`。

## 👥 6. 消费方

- **`scripts/run_plan.py` / `FlowRunner`**：NL flow 主入口，按上面的约定每条 leg 一个扁平目录。
- **`scripts/run_benchmark_test.py`（relay 侧）**：A/B benchmark 的 RelayAgent 半边经 `run_plan.py` 跑 NL flow，于是 flow 自己按上面约定产出 `traj_logs/<ts>_plan_<apps>/NN_<id>/` 每条 leg 一个扁平目录。benchmark 从 stderr 的 `flow traj root:` 定位 flow 根，再 `_harvest_relay_legs` 逐 leg 读 `summary.json`（in-app token）+ `traj.json` 的 `flow_llm_calls`（flow 进程 token）+ `leg_verdict.json` + `agent_reply.json` 汇总。benchmark 的 `relay/` 目录本身只放子进程 `command.json`/`stdout.log`/`stderr.log` + 指向 flow 根的指针。**mw 侧**（MobileWorld）写自己的 `mw/user_task/`，那是外部 runtime 的约定，不归 `RELAY_TRAJ_DIR` 管。
- **standalone `python -m agents.runtime.native_runner <pkg> "<goal>"`**：不设 env，落默认 `traj_logs/user_task/`，享受 backup 轮转。

## 🎛️ 7. 相关 env 速查

| env | 作用 | 默认 |
| --- | --- | --- |
| `RELAY_TRAJ_DIR` | 本次运行的轨迹目录（traj.json + steps + agent_reply）| `traj_logs/user_task` |
| `RELAY_STEP_LOG_DIR` | 只覆写 `steps/` 位置（优先于 `RELAY_TRAJ_DIR`）| 跟随 `RELAY_TRAJ_DIR` |
| `RELAY_STEP_LOG` | `0` 关掉逐步落盘（性能测试必关）| `1` |
| `RELAY_WALL_OUT` | `wall_clock.json` 落点 | `<traj dir>/wall_clock.json` |
| `RELAY_SUMMARY_OUT` | `summary.json` 落点 | 无（不写）|
| `RELAY_REPLY_OUT` | 额外的 reply JSON 落点（给父进程消费）| 无（仍写 `<traj dir>/agent_reply.json`）|
| `RELAY_MW_LLM_CALLS_OUT` | **MobileWorld 基线**进程的逐 LLM call 记录落点（`agents/llm/mw_llm_probe.py` 包 MW 的 chat 调用、只观察不改行为；同时是 `scripts/_mw_probe/sitecustomize.py` 的门控）| 无（探针不激活）|
