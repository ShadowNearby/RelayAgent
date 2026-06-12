# RelayAgent — Claude 项目记忆

## Python 环境

- venv 在 `.venv/`，**Python 3.12**（`pyproject.toml` 锁 `>=3.12,<3.13`，匹配现有 lock）。
- 装依赖（不装本项目，靠 `uv run` 跑源码）：`uv venv --python 3.12 && uv sync --no-install-project --extra dev --extra mw`。`mobile-world` 已移到 optional extra `mw`（只有 A/B baseline / MW 兜底用；入口 import 链不碰它），`jsonschema` 在 extra `dev`（manifest 校验）。CI（`.github/workflows/ci.yml`）只装 `--extra dev`。
- 运行时是纯 Python，无外部 runner、无 server；设备 I/O 经 `agents/device/` 后端抽象（Android=直 adb，见下「Native 运行时」）。
- **无直接 pydantic 依赖**：`action_model.py` 的 `JSONAction` 已是纯 Python（Chaquopy 无 pydantic-core 轮子；行为由 `tests/test_action_model.py` 钉死）。主机上 openai SDK 仍传递性带入 pydantic，但我们的代码不得 import 它。

## LLM 端点

值在 `.env`（gitignore，**别提交、别复述完整 key**）：`LLM_BASE_URL`（LLM 网关）/ `LLM_API_KEY` / `LLM_MODEL`（=`qwen`）。

## 跑测试

**指定 App 调试入口** `python -m agents.native_runner <pkg> "<goal>"`（自己 load `.env`、设 deferred-launch env、激活 AdbKeyboard、进程内跑 `obs→predict→execute` 循环，直 adb）：

```bash
uv run python -m agents.native_runner com.aliyun.tongyi "帮我点三杯蜜雪冰城蜜桃四季春"
uv run python scripts/run_plan.py --yes "帮我找一台适合学生的平板电脑，预算2000以内"   # NL flow 入口（每步用三段式路由选 app + capability）
```

- 旧的测试入口 / NL 入口 / 单 App 脚本入口已删除；新代码直接用 `python -m agents.native_runner`（指定 app）或 `run_plan.py --yes` / `run_plan.py --dry-run`（NL flow）。
- **NL 跨 App Flow 架构**（合成 / 三段式路由 / 校验 / 执行 / leg judge / handoff）详见 [`docs/nl_flow.zh.md`](docs/nl_flow.zh.md)（English: [`docs/nl_flow.md`](docs/nl_flow.md)）；pipeline / CLI 用法 / 缓存 / 真机示例见 [`docs/cross_app_planner.zh.md`](docs/cross_app_planner.zh.md)。
- **capability 不覆盖 → MobileWorld 兜底**：任何 unsatisfiable（coverage gap 修复用尽 / LLM 判整条不可满足 / **repair 用尽仍 invalid** / **stage-3 判设备动作非 foundation 任务**）不再放弃，而是把那条 leg（或整条请求）转成 `type: mobileworld` leg，交给 MobileWorld 无 manifest 的 `general_e2e` 执行（经 `scripts/run_mobileworld.py`，answer 文本回灌 blackboard）。默认开，`RELAY_MW_FALLBACK=0` / `run_plan.py --no-mw-fallback` 关；`RELAY_MW_MAX_ROUND`(25)/`RELAY_MW_TIMEOUT`(600)。详见 nl_flow §10。
  - **stage-3 逃生口**：三段式 router 的 foundation 兜底不再是无条件 catch-all——若任务要求聊天助手做不了的设备/OS 副作用动作（文件管理、改系统设置、驱动别的 App），`_stage3_foundation` 返回 `none` → 抛 `FoundationNotApplicable` → 当 coverage gap → MW，而不是硬塞进 `foundation_llm`。
  - **plan-only tier 用 leg-kind 分档**（`run_benchmark_test.py`）：`covered`（每条 leg 都是真垂类 capability）/ `foundation_fallback`（无 MW leg 但有 `foundation_llm` leg）/ `mw`（全是 MW leg）/ `mixed`（MW + 非 MW 混合）。`plan_summary.json` 的 `mw_fallback` 块给 task 级 + leg 级 MW 占比（`task_touch_rate`/`mw_leg_rate`/`mixed_task_mw_ratios`）。
- 旋钮：`--max-step`（默认 -1 不限）/ `--step_wait_time`（每步 settle，默认 `RELAY_STEP_WAIT` 或 0.5）/ `--keep-ime`（退出不复位输入法）。`RELAY_AGENT_FILE` 换 agent（如 a11y baseline）。

需要 adb + 真机 USB 调试（或模拟器，见 `docs/emulator_testing.zh.md`）。**`agents.native_runner` 自动 `ime enable/set com.android.adbkeyboard/.AdbIME`**（退出 `ime reset` 复位，`--keep-ime` 关）。`RELAY_ANDROID_SERIAL` 选设备。

- **跑前体检**：`uv run python scripts/check_device_env.py [--benchmark ...]`（设备/IME/uiautomator/screencap/App 安装态，端侧需求文档见 `docs/device_setup.zh.md`）。
- **manifest 校验**（schema + prompt_template 规则，CI gate）：`uv run python scripts/validate_manifests.py`。
- **无设备单测**：`uv run python -m unittest discover -s tests -v`（unittest 风格，CI 跑同一条）。

## Native 运行时

运行时是纯 Python，**无 server、无框架冷启动**；设备 I/O 全部走 **`agents/device/` 后端抽象层**（Android=直 adb），由以下模块组成：

- `agents/device/` — **DeviceBackend 抽象层**：`base.py`（ABC + `UINode` 归一化 a11y 节点 + `Key`）、`android.py`（adb 实现，唯一真实后端；含 IME、`dump_ui_tree`、权限弹窗 `dismiss_permission_popup`）、`ios.py`/`harmony.py`（WDA/hdc 骨架，调用抛 NotImplementedError）、`factory.py`（`get_backend()` 按 `RELAY_PLATFORM`（默认 android）分发，serial 是实例属性）、`vendor_profiles.py`（厂商权限表 + `RELAY_VENDOR_PROFILE` JSON overlay）。多平台能力映射见 [`docs/device_backends.zh.md`](docs/device_backends.zh.md)。**`agents/_adb.py` 已退化为同名委托 shim**（旧 import 面兼容；新代码持 backend 实例）。
- `agents/action_model.py` — `JSONAction` + action-type 常量（validator/`__eq__`/`model_dump`）。
- `agents/agent_base.py` — `BaseAgent` + `MCPAgent`（OpenAI client、token 计数、`openai_chat_completions_create` 含 claude/gpt/o1/kimi 分支）。`relay_agent` 经 `from agents.agent_base import MCPAgent as _MCPAgentBase`（别名排序见文件头注释，让 loader 选 RelayAgent）。
- `agents/_img.py` — `pil_to_base64`。
- `agents/native_runtime.py` — **`NativeEnv`**（`JSONAction`→backend 手势：swipe 几何、scroll 方向反转留在这层，tap/键盘/启动落到 backend；`skip_screenshot` 复用上一帧）+ 进程内 `obs→predict→execute` 循环 + **输入通道激活**（Android=AdbKeyboard；不可用时 ASCII 降级 `input text`，中文 loud fail，`native_runner` 对含中文 goal 直接 env_fail 早退）。
- `agents/native_runner.py` — 单 app 模块入口；`run_plan` / `run_benchmark_test` 按 task spawn `python -m agents.native_runner` 子进程（无 server）。agent 经 `RELAY_WALL_OUT`/`RELAY_REPLY_OUT` 落 `wall_clock.json`/`reply.json`，这正是这些消费方读的。

**平台化要点**：manifest 的 `platforms` 字段在 `card_loader.load_all_cards` 按 `RELAY_PLATFORM` 过滤（不含当前平台的卡对路由/规划不可见）；可选 `app_ids: {android, ios, harmonyos}` 映射同一逻辑 App 的多平台 id（`card_loader.resolve_app_id` 解析，fallback `app_id`）；`validate_manifests.py` 对声明 ios/harmonyos 但 selector 只有 `resource_id` 的卡发非致命 WARN。a11y 消费方（grounding/文本 hash/回复 scrape/权限弹窗/a11y baseline）一律吃 `UINode`，不碰 uiautomator XML。状态栏/输入栏裁剪比例 `RELAY_CROP_TOP`(0.08)/`RELAY_CROP_BOTTOM`(0.18) 可调。

## 性能旋钮

每步墙钟 ≈ 几个 sleep + 一次 ~1.5s 截图（**实测：截图是设备/adb 绑定的最大单步成本**）。

| 旋钮 | 默认 | 作用范围 |
| --- | --- | --- |
| `--step_wait_time` / `RELAY_STEP_WAIT` | 0.5 | 每步 observe 前 settle |
| `RELAY_WAIT_SECONDS` | 0.2 | `wait` action 的 sleep（`NativeEnv` 本地读）|
| `RELAY_POLL_SKIP_SLEEP` | 0.3 | wait_for_reply skip 拍 |
| `RELAY_STEP_LOG` | 1（开） | 每步落截图 + action + 点击位置（见下「Step 日志」）。**性能测试设 0 关掉**——它每步写 PNG，tap/swipe 还要重编码一张标注帧，是真实单步开销 |

**录屏跳每步截图（`RELAY_SKIP_STEP_SCREENSHOT`，`run_plan.py --record` 自动开）**：确定性 step 不读 incoming 截图。agent 在 `predict` look-ahead，下一步不在 `_VISION_STEP_KINDS`(=`{wait_for_reply}`) 就打 `skip_screenshot` 标 → `NativeEnv.execute_action` 复用上一帧；打标后睡 `RELAY_BLIND_STEP_SLEEP`（0.15s）吃动画。`tap_text`/`nm_ground_tap` 走 VLM 前自调 `_fresh_vision_frame()` 抓新帧。

> **提速真瓶颈**（非框架）：~1.5s `adb exec-out screencap` 是单步最大成本，直 adb 换不动它；要砍得换流式抓帧后端（minicap/scrcpy，~30-50ms/帧）。这是独立优化，native 里换 `_adb.screencap()` 一个函数即可。

## Adapter 设计要点（`agents/relay_agent.py` + `agents/action_planner.py`）

1. **`open_app` 要 launcher label 不是包名**（千问=`千问`）：`card.embedded_agent.name` → `card.app_name` → 包名。
2. **`tap_text` 优先 uiautomator XML，VLM 兜底。** `_ground_text_via_uiautomator` 按 text/content-desc/resource-id 匹配 bounds 中心。3 次重试 + 0.8s 间隔吃动画。失败路径一律 info/warning 别 debug。
3. **Grounding 输出形态宽**：`_extract_xy()` 容忍 `{x,y}`/`{point:[x,y]}`/`[{x:[x,y]}]`/`{bbox:...}`/纯数字。坐标 `>999` 当像素，否则归一乘 `screen/999`。
4. **冷启动 / deferred-launch**：脚本设 `RELAY_SKIP_OPEN_APP=1` + `RELAY_AGENT_LAUNCH=1`。agent 第一帧 `predict` 调 `_begin_task_once()`：记 `t0` → 起录屏（若 `RELAY_RECORD_DIR`）→ `cold_launch()`（`agents/_adb.py`：force-stop + monkey LAUNCHER + settle 1.0s）。deferred-launch 把启动放到 agent 首帧（native 无框架冷启动，但 IME 激活 / 子进程启动等仍在 t0 前），落在 wall_s 外。atexit 写 `wall_clock.json`（`{wall_s, phase:"task"}`）到 `RELAY_WALL_OUT` 或 `traj_logs/user_task/`。**agent 是 wall_s 唯一写者。** settle 1.0s 清品牌 splash。
5. **fresh conversation**：`build_plan(fresh_conversation=True)` 在 open_app 后插清历史步。`RELAY_FRESH_CONV=0` 关。
6. **`wait_for_reply` 文本-hash 判 done**：done 判定纯靠 uiautomator 文本 hash 稳定性（连续 3 拍 byte-identical），**不调 VLM 判 done**（见代码 `NOTE(no-vlm-done)`：qwen 当 judge 不可靠，对稳定回复反复返回 done=false 吊到超时）。VLM（`_poll_agent_reply`）仅在 scrape 落空时兜底**读回复文本**，只返回 `text`，不再带 `done` 字段。超时按墙钟 `max_seconds = x_max_wait_seconds or max(5×typical_latency, 60)`。text 注入 handoff 的 `ask_user`。
7. **两段式 precheck 省 dump**：Stage 1(~25ms) 截图区域 hash（裁顶 8% 状态栏、底 18% 输入区），变了=streaming 跳过。Stage 2(~2.5s，仅屏稳定才跑) uiautomator 文本 hash，连续 `STABLE_DUMPS_FOR_DONE`(=3) 拍相等才判 done（**不调 VLM**）。熔断：连续 ≥`MAX_DUMP_FAILS`(2) 次 dump 失败关本次 dump。看门狗：连续 ≥`MAX_SKIPS_BEFORE_FORCE`(5) 次 skip 强跑一次文本 dump。
8. **回复文本优先 scrape，VLM 只兜底读文本（不判 done）。** `_extract_reply_text_from_dump`：dump → 按用户气泡 y 切割（`self._last_input_text` 定位）→ 过滤 chrome（`_REPLY_CHROME_LABELS` + streaming markers）→ 丢短 chip（有长节点时剔 <`MIN_CHIP_LEN`(25)）。scrape 落空（如 WebView/canvas 不入 a11y 树，或 `RELAY_SCRAPE=0` 基线）才回落 VLM 读帧。`capture_full` scroll 阶段同样 scrape，失败才回落。
9. **权限弹窗自动 dismiss**：`predict` 入口 `_maybe_dismiss_permission_popup`。先 `dumpsys window` 拿前台包，不在 `_PERMISSION_PACKAGES` 白名单即 fast-exit。命中才 dump，按 `_ALLOW_LABELS`（始终允许>允许>Always allow>...）点 Allow，**永不 Deny**。每 task 上限 8 次。`RELAY_DISMISS_PERMISSIONS=0` 关。

### Manifest 约定

> 本节（语言约定 / `prompt_template` / `x_capture_full_reply` / 卡片 `swipe` 方向 / capability 关键字段）的完整版见 [`docs/manifest_conventions.zh.md`](docs/manifest_conventions.zh.md)（English: [`docs/manifest_conventions.md`](docs/manifest_conventions.md)）。下面是速查要点。

**语言约定 — manifest 用对应 App 的语言写**：英文 App（如 `com.google.android.apps.bard`）的 manifest 用英文写，中文 App（如 `com.autonavi.minimap`）的 manifest 用中文写。

### `prompt_template` — 模板化 submit prompt

结构化能力（导航/订票等）可声明 capability 级 `prompt_template`（+`prompt_slots`），把发给 in-app agent 的措辞固化，LLM 只抽槽位，避免措辞漂移让 App 端意图路由跑偏。缺 **required** 槽**硬失败**；**可选**槽（`required: false`）包进 `[...]` 段，缺值时整段（含周边措辞）删除，例 `Navigate to {place}[ by {mode}].`。**保证边界**：固定的是措辞/意图路由，**槽位取值仍 LLM 抽**（temp=0），不保证取值正确。仅作用于 NL flow（`run_plan.py`/`FlowPlanner`），填充在 `flow_planner._fill_prompt_template` / `_fill_template`。**加载期** `card_catalog._validate_prompt_template` 校验 template↔slot 一致性（未声明占位符/死槽/required 在 `[...]` 内/optional 不在 `[...]`/括号不配对），命中抛 `ManifestValidationError`。详见 [`docs/prompt_template.zh.md`](docs/prompt_template.zh.md)（English: [`docs/prompt_template.md`](docs/prompt_template.md)）。

### `x_capture_full_reply` 开不开？

口诀：**single TextView ⇒ 不开；RecyclerView 多节点 ⇒ 开**。判断：触发回复后 `adb shell uiautomator dump`，1 个长 TextView(>200字)→single-bubble；多个中等节点按卡片排→multi-node。

- **不开**（single-bubble：千问/WPS/携程 QA）：整段在一个 TextView，scrape 一次拿全；要全文调大 `max_seconds`。
- **开**（multi-node 卡片：order_food、高德 find_nearby、携程 search_*、微信 ai_search、XHS QA）：offscreen 卡片被回收须滚动。`max_scrolls`：短 4 / 标准 6 / 多日 8 / 深搜 15。
- **Skip**（短 CTA：高德 navigate_to、WPS ai_ppt、携程 plan_trip）。

**Scroll 幅度** `swipe_down(ratio=0.5)`（clamp `[0.1, 0.5]`），`RELAY_CAPTURE_SCROLL_RATIO` 覆写（同样被 clamp 到 ≤0.5）。大→省 VLM 但 seam 丢词；小→重叠多更稳。chunks 按捕获顺序拼接。

**公平开关 `RELAY_CAPTURE_FULL_REPLY`（默认 1 开）**：MW 基线 `general_e2e` 无滚动捕获——回复看着稳了就读**当前可见帧**文本然后 `answer`。所以 A/B 时把它设 `0`：`wait_for_reply` 在"屏幕文本 hash 稳定"判 done 后**直接返回首帧可见文本**，不进 scrolling 捕获相（`_materialize` 里 `capture_full = p["capture_full"] and self.capture_full_enabled`）。`run_benchmark_test.py` **默认 OFF**（同 `RELAY_ROUTE_OVERLAY`/`RELAY_STEP_LOG`），`--full-reply` 才开回。

**卡片 `swipe` → scroll 动作（含方向反转）**：manifest 里的 `swipe: <direction>` 按 **scroll/内容移动方向** 写，不按手指滑动方向写。它经 `action_planner` 编成逻辑 `swipe` step，`_materialize` 发成 `scroll` 动作，于是 `NativeEnv._dispatch` 对 up/down 做反转后再落到底层 adb 手势：

- `swipe: up` → `scroll(direction="up")` → 内容上移/视觉向上滚 → 底层实际手势是 **手指向下滑**。
- `swipe: down` → `scroll(direction="down")` → 内容下移/视觉向下滚 → 底层实际手势是 **手指向上滑**。
- `left`/`right` 目前不反转。写卡片时统一按 scroll 语义思考。

### `predict` 多次返回同一步

`wait_for_reply` 不推进 cursor（`_materialize` 返回 `advance=False`），runner 反复 predict 拿下一 poll。故 runner step 计数 ≠ plan cursor；thought 带 `[hold]` 表示 cursor 没推进；同一 step 号在 traj 多次出现正常。

## Trajectory 日志目录

> 完整约定（三写入方 / 轮转 / leg 目录形态 / `flow_llm_calls` / 消费方 / env 速查）见 [`docs/trajectory_logging.zh.md`](docs/trajectory_logging.zh.md)（English: [`docs/trajectory_logging.md`](docs/trajectory_logging.md)）。下面是速查要点。

**输出目录由 `RELAY_TRAJ_DIR` 决定**（traj.json + `steps/` + `agent_reply.json` 都落这里；`native_runner.TRAJ_DIR` / `relay_agent._TRAJ_DIR` / `StepLogger` 三处统一读它）：

- **不设**（standalone `python -m agents.native_runner`）：默认 `traj_logs/user_task/`。每次启动（`_rotate_traj_dir`）把上次 `traj_logs/user_task/` 搬到 `traj_logs/user_task_backup_<ts>/`，再 mkdir + seed 空 `traj.json`。`ls -td ...backup_* | head -1` 是**上一次**的内容。
- **设**（NL flow 每条 leg，`flow_runner` 设 `RELAY_TRAJ_DIR=<flow_root>/NN_<id>`）：子进程直接写进该 leg 目录，**不碰全局 `user_task`、不轮转、无 copytree**。flow 输出形如 `traj_logs/<ts>_plan_<apps>/NN_<id>/{traj.json, steps/, agent_reply.json, wall_clock.json, summary.json, leg_verdict.json}`（**没有 `user_task/` 子层**）。`wall_clock.json`/`summary.json` 仍各经 `RELAY_WALL_OUT`/`RELAY_SUMMARY_OUT` 指到同一 leg 目录。
- flow 级 LLM call（leg judge / bind extract）由 `FlowRunner._RecordingLLM` 记录，fold 进每条 leg 的 `traj.json` 顶层 `flow_llm_calls`（与 in-app agent 的 `["0"]["llm_calls"]` 区分）。
- `scripts/run_benchmark_test.py` 的 **relay 侧**经 `run_plan.py` 跑 NL flow，从产出的每条 leg 目录 harvest（`summary.json` + `traj.json` 的 `flow_llm_calls` + `leg_verdict.json` + `agent_reply.json`）；**mw 侧**（MobileWorld）写自己的 `mw/user_task/`，是外部 runtime 约定，不归 `RELAY_TRAJ_DIR` 管。

### Step 日志（逐步轨迹）

`agents/native_runtime.py:StepLogger`，在 `run_task` 循环里每步落盘，**默认开**：记下 agent 这一步**看到的截图**、它返回的 **action**、以及**点击位置**。

- 落 `<RELAY_TRAJ_DIR>/steps/`（跟 traj.json 同目录；standalone 默认 `traj_logs/user_task/steps/`，flow 则在 leg 目录下）。`RELAY_STEP_LOG_DIR` 显式覆写优先级最高。
- 每步：`step_<n>.png`（agent 行动所依据的那帧，pre-action obs，所以 action 的 `(x,y)` 就落在这帧上）+ `step_<n>_marked.png`（在帧上画标记：tap/long_press/double_tap 画红点+十字，swipe/scroll/drag 画红箭头；无坐标的 action 如 input_text 不出 marked 帧）+ `steps.json`（索引：step、ts、action_type、完整 action dict、`click=[x,y]`、agent 的 thought、两张图文件名）。`steps.json` 每步整体重写，跑崩了也是合法 JSON。
- best-effort：`record` 包 try/except，落盘失败只 warning，绝不打断 obs→predict→execute 循环。
- **`RELAY_STEP_LOG=0` 关掉**——**性能测试必关**：每步一次 PNG 写盘，tap/swipe 还要 `convert('RGB')`+重编码一张标注帧，是真实单步开销，会污染墙钟。

## Handoff

最后一步调 `ask_user` 等终端输入。stdin 被重定向时以 `EOF when reading a line` 结束 —— 这是**成功**不是失败。

## Android App 移植（android/，进行中）

纯无障碍方案 + Chaquopy 嵌 Python，把整个 NL flow 装进独立 App（无电脑无 adb）。骨架与映射表见 [`android/README.md`](android/README.md)。**主机行为零漂移**：以下接缝默认值全部保持原行为，只有 Android 侧换实现。

- **LLM client**：一律经 `agents/llm_client.py:make_llm_client`（主机=真 openai SDK；`RELAY_LLM_HTTP=1` 或 SDK 缺失=stdlib HTTP shim，无 streaming）。`JSONAction` 已去 pydantic（纯 Python，行为由 `tests/test_action_model.py` 钉死）。
- **交互**：终端 `input()` 已抽成 `agents/interaction.py:InteractionProvider`（`ask_user` 返回 None=EOF/接管=handoff 成功终止；`should_stop` 在循环边界轮询）。Android 实现=悬浮窗。
- **leg 执行**：`flow_runner` 经 LegExecutor 接缝——`SubprocessLegExecutor`（默认，字节级等价）/ `InProcessLegExecutor`（`RELAY_LEG_EXECUTOR=inprocess`，Chaquopy 无法 spawn 子进程；env 快照/还原）。`native_runner.run_leg()` 可 import，`RELAY_TRAJ_DIR` 按调用解析（不再 import 期冻结）。
- **NL pipeline**：`agents/nl_flow.py:plan_request/execute_plan`（结构化结果），`run_plan.py` 只剩 CLI 前端。`plan_request(allow_mw_legs=False)`=Android 禁缓存 MW plan；`FlowPlanner(mw_fallback=False)`=不可覆盖 leg 直接 unsatisfiable。
- **路径**：`RELAY_TRAJ_ROOT` 重定向 traj_logs 基目录（Android 指 filesDir；主机默认 `<repo>/traj_logs` 不变）。
- **Spike B 工具**：`scripts/diff_a11y_dump.py` 对比 App 内 a11y 序列化 与真 `uiautomator dump` 的 (text/content-desc/bounds) 节点集 + text-hash 流。
- **已知语义漂移（端侧接受）**：无 shell 拿不到真 force-stop，冷启动以 `FLAG_ACTIVITY_CLEAR_TASK` 重启近似——端上运行不与 benchmark 对比。
- **待接线**：`relay_android/backend.py:install()` 等 `agents.device` 注入缝（P0.1，device-backend 分支）落地。
