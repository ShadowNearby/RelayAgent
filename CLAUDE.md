# RelayAgent — Claude 项目记忆

## Python 环境

- venv 在 `.venv/`，**Python 3.12**（`pyproject.toml` 锁 `>=3.12,<3.13`，匹配现有 lock）。
- 装依赖（不装本项目，靠 `uv run` 跑源码）：`uv venv --python 3.12 && uv sync --no-install-project`。
- 运行时是纯 Python over adb，无外部 runner、无 server（见下「Native 运行时」）。
- pydantic 锁 `<2.11`：保守保留以对齐已解析的 lock，`action_model.py` 的 `JSONAction` 用它。

## LLM 端点

值在 `.env`（gitignore，**别提交、别复述完整 key**）：`LLM_BASE_URL`（SJTU IPADS 网关）/ `LLM_API_KEY` / `LLM_MODEL`（=`qwen`）。

## 跑测试

**首选入口** `scripts/run_native.py <pkg> "<goal>"`（自己 load `.env`、设 deferred-launch env、激活 AdbKeyboard、进程内跑 `obs→predict→execute` 循环，直 adb）：

```bash
uv run python scripts/run_native.py com.aliyun.tongyi "帮我点三杯蜜雪冰城蜜桃四季春"
uv run python scripts/run_nl.py "帮我找一台适合学生的平板电脑，预算2000以内"   # 单 App NL 路由（选 app + capability）
```

- `scripts/run_test.py` 现在是**指向 `run_native.py` 的弃用 shim**（保留旧调用习惯），新代码直接用 `run_native.py`。
- 旋钮：`--max-step`（默认 -1 不限）/ `--step_wait_time`（每步 settle，默认 `RELAY_STEP_WAIT` 或 0.2）/ `--keep-ime`（退出不复位输入法）。`RELAY_AGENT_FILE` 换 agent（如 a11y baseline）。

需要 adb + 真机 USB 调试。**`run_native` 自动 `ime enable/set com.android.adbkeyboard/.AdbIME`**（退出 `ime reset` 复位，`--keep-ime` 关）。`RELAY_ANDROID_SERIAL` 选设备。

## Native 运行时

运行时是纯 Python over adb，**无 server、无框架冷启动**，由以下模块组成：

- `agents/action_model.py` — `JSONAction` + action-type 常量（validator/`__eq__`/`model_dump`）。
- `agents/agent_base.py` — `BaseAgent` + `MCPAgent`（OpenAI client、token 计数、`openai_chat_completions_create` 含 claude/gpt/o1/kimi 分支）。`relay_agent` 经 `from agents.agent_base import MCPAgent as _MCPAgentBase`（别名排序见文件头注释，让 loader 选 RelayAgent）。
- `agents/_img.py` — `pil_to_base64`。
- `agents/native_runtime.py` — **`NativeEnv`**（`JSONAction`→直 adb：swipe 几何、scroll 方向反转、`ADB_INPUT_B64` 键盘广播、`skip_screenshot` 复用上一帧）+ 进程内 `obs→predict→execute` 循环 + **AdbKeyboard IME 激活**。
- `scripts/run_native.py` — 单 app 入口；`run_nl` / `run_single_app_benchmark` 按 task spawn `run_native` 子进程（无 server）。agent 经 `RELAY_WALL_OUT`/`RELAY_REPLY_OUT` 落 `wall_clock.json`/`reply.json`，这正是这些消费方读的。

## 性能旋钮

每步墙钟 ≈ 几个 sleep + 一次 ~1.5s 截图（**实测：截图是设备/adb 绑定的最大单步成本**）。

| 旋钮 | 默认 | 作用范围 |
| --- | --- | --- |
| `--step_wait_time` / `RELAY_STEP_WAIT` | 0.2 | 每步 observe 前 settle |
| `RELAY_WAIT_SECONDS` | 0.2 | `wait` action 的 sleep（`NativeEnv` 本地读）|
| `RELAY_POLL_SKIP_SLEEP` | 0.3 | wait_for_reply skip 拍 |
| `RELAY_STEP_LOG` | 1（开） | 每步落截图 + action + 点击位置（见下「Step 日志」）。**性能测试设 0 关掉**——它每步写 PNG，tap/swipe 还要重编码一张标注帧，是真实单步开销 |

**录屏跳每步截图（`RELAY_SKIP_STEP_SCREENSHOT`，`run_nl.py --record` 自动开）**：确定性 step 不读 incoming 截图。agent 在 `predict` look-ahead，下一步不在 `_VISION_STEP_KINDS`(=`{wait_for_reply}`) 就打 `skip_screenshot` 标 → `NativeEnv.execute_action` 复用上一帧；打标后睡 `RELAY_BLIND_STEP_SLEEP`（0.15s）吃动画。`tap_text`/`nm_ground_tap` 走 VLM 前自调 `_fresh_vision_frame()` 抓新帧。

> **提速真瓶颈**（非框架）：~1.5s `adb exec-out screencap` 是单步最大成本，直 adb 换不动它；要砍得换流式抓帧后端（minicap/scrcpy，~30-50ms/帧）。这是独立优化，native 里换 `_adb.screencap()` 一个函数即可。

## Adapter 设计要点（`agents/relay_agent.py` + `agents/action_planner.py`）

1. **`open_app` 要 launcher label 不是包名**（千问=`千问`）：`card.embedded_agent.name` → `card.app_name` → 包名。
2. **`tap_text` 优先 uiautomator XML，VLM 兜底。** `_ground_text_via_uiautomator` 按 text/content-desc/resource-id 匹配 bounds 中心。3 次重试 + 0.8s 间隔吃动画。失败路径一律 info/warning 别 debug。
3. **Grounding 输出形态宽**：`_extract_xy()` 容忍 `{x,y}`/`{point:[x,y]}`/`[{x:[x,y]}]`/`{bbox:...}`/纯数字。坐标 `>999` 当像素，否则归一乘 `screen/999`。
4. **冷启动 / deferred-launch**：脚本设 `RELAY_SKIP_OPEN_APP=1` + `RELAY_AGENT_LAUNCH=1`。agent 第一帧 `predict` 调 `_begin_task_once()`：记 `t0` → 起录屏（若 `RELAY_RECORD_DIR`）→ `cold_launch()`（`agents/_adb.py`：force-stop + monkey LAUNCHER + settle 1.0s）。deferred-launch 把启动放到 agent 首帧（native 无框架冷启动，但 IME 激活 / 子进程启动等仍在 t0 前），落在 wall_s 外。atexit 写 `wall_clock.json`（`{wall_s, phase:"task"}`）到 `RELAY_WALL_OUT` 或 `traj_logs/user_task/`。**agent 是 wall_s 唯一写者。** settle 1.0s 清品牌 splash。
5. **fresh conversation**：`build_plan(fresh_conversation=True)` 在 open_app 后插清历史步。`RELAY_FRESH_CONV=0` 关。
6. **`wait_for_reply` 文本-hash 判 done**：done 判定纯靠 uiautomator 文本 hash 稳定性（连续 3 拍 byte-identical），**不调 VLM 判 done**（见代码 `TODO(reply-done-vlm)`：qwen 当 judge 不可靠，对稳定回复反复返回 done=false 吊到超时）。VLM 仅在 scrape 落空时兜底**读回复文本**（prompt `_REPLY_WATCH_SYSTEM` 仍带 done 半段，但两个调用点都丢弃 `done`，属待清理的死意图）。超时按墙钟 `max_seconds = x_max_wait_seconds or max(5×typical_latency, 60)`。text 注入 handoff 的 `ask_user`。
7. **两段式 precheck 省 dump**：Stage 1(~25ms) 截图区域 hash（裁顶 8% 状态栏、底 18% 输入区），变了=streaming 跳过。Stage 2(~2.5s，仅屏稳定才跑) uiautomator 文本 hash，连续 `STABLE_DUMPS_FOR_DONE`(=3) 拍相等才判 done（**不调 VLM**）。熔断：连续 ≥`MAX_DUMP_FAILS`(2) 次 dump 失败关本次 dump。看门狗：连续 ≥`MAX_SKIPS_BEFORE_FORCE`(5) 次 skip 强跑一次文本 dump。
8. **回复文本优先 scrape，VLM 只兜底读文本（不判 done）。** `_extract_reply_text_from_dump`：dump → 按用户气泡 y 切割（`self._last_input_text` 定位）→ 过滤 chrome（`_REPLY_CHROME_LABELS` + streaming markers）→ 丢短 chip（有长节点时剔 <`MIN_CHIP_LEN`(25)）。scrape 落空（如 WebView/canvas 不入 a11y 树，或 `RELAY_SCRAPE=0` 基线）才回落 VLM 读帧。`capture_full` scroll 阶段同样 scrape，失败才回落。
9. **权限弹窗自动 dismiss**：`predict` 入口 `_maybe_dismiss_permission_popup`。先 `dumpsys window` 拿前台包，不在 `_PERMISSION_PACKAGES` 白名单即 fast-exit。命中才 dump，按 `_ALLOW_LABELS`（始终允许>允许>Always allow>...）点 Allow，**永不 Deny**。每 task 上限 8 次。`RELAY_DISMISS_PERMISSIONS=0` 关。

### `x_capture_full_reply` 开不开？

口诀：**single TextView ⇒ 不开；RecyclerView 多节点 ⇒ 开**。判断：触发回复后 `adb shell uiautomator dump`，1 个长 TextView(>200字)→single-bubble；多个中等节点按卡片排→multi-node。

- **不开**（single-bubble：千问/WPS/携程 QA）：整段在一个 TextView，scrape 一次拿全；要全文调大 `max_seconds`。
- **开**（multi-node 卡片：order_food、高德 find_nearby、携程 search_*、微信 ai_search、XHS QA）：offscreen 卡片被回收须滚动。`max_scrolls`：短 4 / 标准 6 / 多日 8 / 深搜 15。
- **Skip**（短 CTA：高德 navigate_to、WPS ai_ppt、携程 plan_trip）。

**Scroll 幅度** `swipe_down(ratio=0.5)`（clamp `[0.1, 0.5]`），`RELAY_CAPTURE_SCROLL_RATIO` 覆写（同样被 clamp 到 ≤0.5）。大→省 VLM 但 seam 丢词；小→重叠多更稳。chunks 按捕获顺序拼接。

**卡片 `swipe` → scroll 动作（含方向反转）**：卡片里的 `swipe: <direction>` 经 `action_planner` 编成逻辑 `swipe` step，`_materialize` 发成 `scroll` 动作，于是 `NativeEnv._dispatch` 对 up/down 做反转。即卡片写 `swipe: up` 实际手势是 down 方向（scroll up = 内容上移 = 视觉向上滚）；写卡片时按 scroll 语义思考。

### `predict` 多次返回同一步

`wait_for_reply` 不推进 cursor（`_materialize` 返回 `advance=False`），runner 反复 predict 拿下一 poll。故 runner step 计数 ≠ plan cursor；thought 带 `[hold]` 表示 cursor 没推进；同一 step 号在 traj 多次出现正常。

## Trajectory 日志目录

每次 `run_native` 启动（`_rotate_traj_dir`）把上次 `traj_logs/user_task/` 搬到 `traj_logs/user_task_backup_<ts>/`（ts = 新跑启动时刻），再 mkdir + seed 空 `traj.json`（供 agent `_append_llm_call`）。**本次输出永远在 `traj_logs/user_task/`**；`ls -td ...backup_* | head -1` 是**上一次**的内容。

### Step 日志（逐步轨迹）

`agents/native_runtime.py:StepLogger`，在 `run_task` 循环里每步落盘，**默认开**：记下 agent 这一步**看到的截图**、它返回的 **action**、以及**点击位置**。

- 落 `traj_logs/user_task/steps/`（跟 traj.json 同目录，享受同样的 backup 轮转）。`RELAY_STEP_LOG_DIR` 可改目录。
- 每步：`step_<n>.png`（agent 行动所依据的那帧，pre-action obs，所以 action 的 `(x,y)` 就落在这帧上）+ `step_<n>_marked.png`（在帧上画标记：tap/long_press/double_tap 画红点+十字，swipe/scroll/drag 画红箭头；无坐标的 action 如 input_text 不出 marked 帧）+ `steps.json`（索引：step、ts、action_type、完整 action dict、`click=[x,y]`、agent 的 thought、两张图文件名）。`steps.json` 每步整体重写，跑崩了也是合法 JSON。
- best-effort：`record` 包 try/except，落盘失败只 warning，绝不打断 obs→predict→execute 循环。
- **`RELAY_STEP_LOG=0` 关掉**——**性能测试必关**：每步一次 PNG 写盘，tap/swipe 还要 `convert('RGB')`+重编码一张标注帧，是真实单步开销，会污染墙钟。

## Handoff

最后一步调 `ask_user` 等终端输入。stdin 被重定向时以 `EOF when reading a line` 结束 —— 这是**成功**不是失败。
