# Trajectory logging convention

> 中文: [`trajectory_logging.zh.md`](trajectory_logging.zh.md)

> Where a run's trajectory lands, what the directory looks like, and who reads
> it. The core is **one env var, `RELAY_TRAJ_DIR`**, that every writer honors;
> the NL flow / benchmark pin it to each leg's own dir, so there is no global
> scratch, no `user_task/` nesting, and no copytree.

---

## 1. One line

**`RELAY_TRAJ_DIR` decides this run's trajectory dir** (`traj.json` + `steps/` +
`agent_reply.json` all land there).

- **Unset** → defaults to `traj_logs/user_task/`, for standalone debugging; the
  previous run is rotated aside to `user_task_backup_<ts>/` at startup.
- **Set** → the subprocess writes straight into that dir, with **no rotation, no
  global `user_task`, no copytree**. The NL flow pins one per leg:
  `traj_logs/<ts>_plan_<apps>/NN_<id>/`.

## 2. Three writers read the same env

| Writer | Writes | How it picks the dir |
| --- | --- | --- |
| `agents/runtime/native_runner.py:TRAJ_DIR` | seeds empty `traj.json` + rotation logic | `RELAY_TRAJ_DIR` or default `traj_logs/user_task` |
| `agents/agent/relay_agent.py:_TRAJ_DIR` | `_append_llm_call` → `traj.json`; `_maybe_persist_reply` → `agent_reply.json` | same |
| `agents/runtime/native_runtime.py:StepLogger` | `steps/` (per-step PNGs + `steps.json`) | `RELAY_STEP_LOG_DIR` (explicit override, highest) > `RELAY_TRAJ_DIR` > default |

`wall_clock.json` / `summary.json` / an external `reply.json` keep their own
explicit envs (`RELAY_WALL_OUT` / `RELAY_SUMMARY_OUT` / `RELAY_REPLY_OUT`) —
callers usually point those at the same leg dir too.

## 3. Rotation: standalone only

`native_runner._rotate_traj_dir`:

- **`RELAY_TRAJ_DIR` unset** (standalone): if `traj_logs/user_task/` already
  exists → rename it to `traj_logs/user_task_backup_<ts>/` (ts = this run's start),
  then mkdir + seed an empty `traj.json`. `ls -td ...backup_* | head -1` is the
  **previous** run.
- **Set** (flow/benchmark leg): each leg dir is already unique, so **skip the
  backup rename** — just mkdir + seed.

## 4. Layout of one NL-flow leg

`flow_runner` sets `RELAY_TRAJ_DIR=<flow_root>/NN_<id>` for the subprocess, so:

```
traj_logs/plan_bard_20260608_231751/
  01_plan_route/
    traj.json          in-app agent's ["0"]["llm_calls"] + top-level flow_llm_calls
    steps/             step_<n>.png + step_<n>_marked.png + steps.json
    agent_reply.json   the in-app reply the agent scraped {reply, target_app}
    wall_clock.json    {wall_s, phase:"task"}  (RELAY_WALL_OUT)
    summary.json       {steps, last_action_type, last_goal_status, token_usage}
    leg_verdict.json   leg judge verdict {status, score, reason}  (flow writes)
```

There is **no `01_plan_route/user_task/` layer**, and the flow never creates or
touches the global `traj_logs/user_task/`.

## 5. `flow_llm_calls` — the flow process's LLM calls land in the leg too

The in-app agent's LLM calls are written by `relay_agent` to `traj.json`'s
`["0"]["llm_calls"]`. But the **flow process** itself also calls the LLM:

- **leg judge** (`agents/flow/leg_judge.py`, with screenshots; may run multiple times
  on a `loading` retry)
- **bind extract** (`flow_runner._extract`, text-only slot extraction)

Those go through a **raw OpenAI client** in the flow process — they don't pass
through the agent's instrumented wrapper and historically landed in no traj.
`FlowRunner` now wraps that client with `_RecordingLLM`: every
`chat.completions.create` is recorded (`purpose` / `model` / sanitized
`messages` — base64 screenshots become `<base64 image, N chars>` / `usage`
tokens / `elapsed_s` / `response`), and the per-leg slice is folded into that
leg's `traj.json` under the **top-level `flow_llm_calls`** key (distinct from the
in-app `["0"]["llm_calls"]`). `purpose` is stamped at the call site
(`leg_judge` / `bind_extract`).

> Note: the flow **planning** phase (synthesis and three-stage routing fallback
> in `run_plan.py` / `FlowPlanner`) is a separate stage; its LLM calls do **not**
> go through `FlowRunner._RecordingLLM` and are not in `flow_llm_calls` today.

## 6. Consumers

- **`scripts/run_plan.py` / `FlowRunner`**: the NL-flow entry point; one flat dir
  per leg per the convention above.
- **`scripts/run_benchmark_test.py` (relay side)**: the RelayAgent half of the
  A/B benchmark runs the task through `run_plan.py`'s NL flow, so the flow emits
  the standard one-flat-dir-per-leg tree `traj_logs/<ts>_plan_<apps>/NN_<id>/`.
  The benchmark locates the flow root from the `flow traj root:` stderr line,
  then `_harvest_relay_legs` reads each leg's `summary.json` (in-app tokens) +
  `traj.json`'s `flow_llm_calls` (flow-process tokens) + `leg_verdict.json` +
  `agent_reply.json`. The benchmark's own `relay/` dir holds only the
  subprocess `command.json`/`stdout.log`/`stderr.log` plus a pointer to the flow
  root. The **mw side** (MobileWorld) writes its own `mw/user_task/` — that is
  the external runtime's convention and is not governed by `RELAY_TRAJ_DIR`.
- **standalone `python -m agents.runtime.native_runner <pkg> "<goal>"`**: leaves the env
  unset, lands in the default `traj_logs/user_task/`, with backup rotation.

## 7. Related env cheat-sheet

| env | Effect | Default |
| --- | --- | --- |
| `RELAY_TRAJ_DIR` | this run's trajectory dir (traj.json + steps + agent_reply) | `traj_logs/user_task` |
| `RELAY_STEP_LOG_DIR` | override `steps/` location only (wins over `RELAY_TRAJ_DIR`) | follows `RELAY_TRAJ_DIR` |
| `RELAY_STEP_LOG` | `0` disables per-step dumps (turn off for perf tests) | `1` |
| `RELAY_WALL_OUT` | where `wall_clock.json` lands | `<traj dir>/wall_clock.json` |
| `RELAY_SUMMARY_OUT` | where `summary.json` lands | none (not written) |
| `RELAY_REPLY_OUT` | extra reply-JSON sink (for a parent process) | none (still writes `<traj dir>/agent_reply.json`) |
