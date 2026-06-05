# RelayAgent — Claude 项目记忆

## Python 环境

- venv 在 `.venv/`，**Python 3.12**（MobileWorld 要求 `>=3.12,<3.13`）。
- 装依赖（不装本项目，靠 `uv run` 跑源码）：`uv venv --python 3.12 && uv sync --no-install-project`。
- MobileWorld 经 `pyproject.toml` 的 git 依赖 + `[tool.uv.sources]` 自动 clone，`mw` / `mobile-world` 可用。
- pydantic 锁 `<2.11`（fastmcp 2.9.2 不兼容 `>=2.11`），写在 `pyproject.toml`，别动。

## LLM 端点

值在 `.env`（gitignore，**别提交、别复述完整 key**）：`LLM_BASE_URL`（SJTU IPADS 网关）/ `LLM_API_KEY` / `LLM_MODEL`（=`qwen`）。

## 跑测试

**首选入口** `scripts/run_test.py`（自己 load `.env`、设 deferred-launch env、转发 flag 给 `mw test`）：

```bash
uv run python scripts/run_test.py com.aliyun.tongyi "帮我点三杯蜜雪冰城蜜桃四季春"
uv run python scripts/run_nl.py "在北京找三家独立书店，挑一家打车过去"   # 多 app NL 路由（选既有 flow / 单 app）
uv run python scripts/run_flow.py manifests/_flows/xhs_to_amap_place.yaml --nl "..."  # 跑手写 flow
uv run python scripts/run_plan.py "在上海找三家小众书店，挑一家打车过去" --dry-run  # 自动合成跨 app plan
```

直接 `mw test`（仅调试）：参数名须匹配 `RelayAgent.__init__`：`--model_name` / `--llm_base_url` / `--api_key`（**不是** `--base_url`），并 `export RELAY_TARGET_APP=<pkg>`。绕过脚本时 planner 会自己发 `open_app` 步。

需要 adb + 真机 USB 调试 + `com.android.adbkeyboard/.AdbIME`。`RELAY_ANDROID_SERIAL` 选设备。

## 自动跨 App 规划（`run_plan.py` / `FlowPlanner`）

**详细设计 + 用法见 [`docs/cross_app_planner.md`](docs/cross_app_planner.md)。** 速览：

- 定位：`run_nl` 是在**手写** flow / 单 app 里**选**；`run_flow` 跑**手写** flow；**`run_plan` 在没有对应手写 flow 时让 LLM 现场合成**一条跨 app plan。三者共用 `FlowRunner` 与 flow schema。
- 链路（`scripts/run_plan.py` + `agents/flow_planner.py`）：`build_catalog`（复用 run_nl）→ 缓存查找 → `FlowPlanner.plan`（LLM→fenced JSON→本地校验）→ 落盘 `manifests/_generated/`（gitignore）→ 预览 + 确认（默认 N，非交互 EOF=不执行）→ `FlowRunner.run()`。
- 生成的 plan **复用 flow YAML 形状但无 `inputs` 块**（值直接烤进 prompt）；leg 间数据流仍 `extract`/`bind`/`{var}`。
- 校验挡：未知 app/capability id、悬空 `{var}`、`bind`/`id` 重复、**`handoff_to_user_required` 非末尾必须紧跟 `ask_user`（末尾可作终点）**。
- **handoff 往返 = 先 A 后 B**：A（已落地）handoff leg → 流程 `ask_user` → 全新 `mw test` leg 消费回答；B（仅留缝，`# TODO(phase-B):`）同会话原地续跑。
- 故意留的 TODO：**repair 重修循环**（校验失败硬报错代替，`_repair` 空壳）、**语义缓存复用**（仅精确串匹配）。
- 旋钮：`--dry-run`（只规划+预览）/ `--yes`（跳确认）/ `--no-cache`（强制重生成）/ `--record` / `-- <透传 mw test>`。批跑时 stdin `</dev/null` → 中途选单步自动取第一候选。

## MobileWorld fork

当前 pin `ShadowNearby/MobileWorld@73c8c1b`（`relay-patch`）= 上游 + 四个 patch（默认对上游无影响）：

1. server `WAIT` 硬编码 sleep 改读 `MW_WAIT_SECONDS`。
2. `execute_adb` 加 `MW_ADB_TIMEOUT`（默认 30s），防 adb 卡死永久阻塞。
3. **（关键）** 自起 server 的 stdout/stderr 重定向到 `$TMPDIR/mw_server_<port>.log`，原 PIPE 不排空会卡死 server。
4. `execute_action` 读 `action.action_json["skip_screenshot"]`：为真则复用上一帧，不重拍。

回上游：`mobile-world` 指回 `Tongyi-MAI/MobileWorld` 并 rebase 四 patch。升级：改 `pyproject.toml` 的 `rev` 后 `uv sync`。

### 持久化 server（默认复用）

四个入口脚本 spawn `mw test` 前经 `scripts/_mw_server.py:ensure_server()` 探测 6800：健在就注入 `--aw_host http://localhost:6800` 复用；没有就脱离会话起一个常驻的（outlive 本次 run，pid 写 `$TMPDIR/mw_server_6800.pid`）。省掉每 run 自起/销毁开销。

- **server 端 env 只在启动那刻烤入**（`MW_WAIT_SECONDS` / `MW_ADB_TIMEOUT`）→ 复用现有 server 时 per-run 改 `MW_*` **不生效**。
- 改了 server 端代码 / fork patch / 想重烤 `MW_*` → `RELAY_MW_SERVER_RESTART=1` 或 `kill -9 $(cat $TMPDIR/mw_server_6800.pid)`。
- 旋钮：`RELAY_AW_HOST=<url>` 直接指定；`RELAY_NO_PERSIST_SERVER=1` 退回每 run 自起；调用方自传 `--aw_host` 时不干预。
- 注意：agent/adapter 是 client 侧不受旧 server 影响，只有 fork 四 patch 在 server 侧。排查 `ss -ltnp | grep 6800`。

## 性能旋钮

每步墙钟 ≈ 三个 sleep + 一次 ~0.85s 截图：

| 旋钮 | 默认（MW 原值） | 作用范围 |
| --- | --- | --- |
| `--step_wait_time` | 0.2（1.0） | 每步 observe 前 |
| `MW_WAIT_SECONDS` | 0.2（1.0） | 只 wait/poll 拍 |
| `RELAY_POLL_SKIP_SLEEP` | 0.3 | wait_for_reply skip 拍 |

**录屏跳每步截图（`RELAY_SKIP_STEP_SCREENSHOT`，`run_nl.py --record` 自动开）**：确定性 step 不读 incoming 截图。agent 在 `predict` look-ahead，下一步不在 `_VISION_STEP_KINDS`(=`{wait_for_reply}`) 就打 `skip_screenshot` 标 → fork patch #4 复用上一帧；打标后睡 `RELAY_BLIND_STEP_SLEEP`（0.15s）吃动画。`tap_text`/`nm_ground_tap` 走 VLM 前自调 `_fresh_vision_frame()` 抓新帧。

## Adapter 设计要点（`agents/relay_agent.py` + `agents/action_planner.py`）

1. **`open_app` 要 launcher label 不是包名**（千问=`千问`）：`card.embedded_agent.name` → `card.app_name` → 包名。
2. **`tap_text` 优先 uiautomator XML，VLM 兜底。** `_ground_text_via_uiautomator` 按 text/content-desc/resource-id 匹配 bounds 中心。3 次重试 + 0.8s 间隔吃动画。失败路径一律 info/warning 别 debug。
3. **Grounding 输出形态宽**：`_extract_xy()` 容忍 `{x,y}`/`{point:[x,y]}`/`[{x:[x,y]}]`/`{bbox:...}`/纯数字。坐标 `>999` 当像素，否则归一乘 `screen/999`。
4. **冷启动 / deferred-launch**：脚本设 `RELAY_SKIP_OPEN_APP=1` + `RELAY_AGENT_LAUNCH=1`。agent 第一帧 `predict` 调 `_begin_task_once()`：记 `t0` → 起录屏（若 `RELAY_RECORD_DIR`）→ `cold_launch()`（`agents/_adb.py`：force-stop + monkey LAUNCHER + settle 1.0s）。框架 ~2.7s 冷启动排除在 wall_s 外。atexit 写 `wall_clock.json`（`{wall_s, phase:"task"}`）到 `RELAY_WALL_OUT` 或 `traj_logs/user_task/`。**agent 是 wall_s 唯一写者。** settle 1.0s 清品牌 splash。
5. **fresh conversation**：`build_plan(fresh_conversation=True)` 在 open_app 后插清历史步。`RELAY_FRESH_CONV=0` 关。
6. **`wait_for_reply` VLM 轮询**：系统 prompt `_REPLY_WATCH_SYSTEM`，VLM 回 `{done, text}`。`done=True && text==None` 视为不可信继续 poll。超时按墙钟 `max_seconds = x_max_wait_seconds or max(5×typical_latency, 60)`。text 注入 handoff 的 `ask_user`。
7. **两段式 precheck 省 VLM**：Stage 1(~25ms) 截图区域 hash（裁顶 8% 状态栏、底 18% 输入区），变了=streaming 跳过。Stage 2(~2.5s，仅屏稳定才跑) uiautomator 文本 hash，连续两拍相等才调 VLM 判 done。熔断：连续 ≥2 次 dump 失败关本次 dump。看门狗：连续 ≥5 次 skip 强跑一次 VLM。
8. **回复文本优先 scrape，VLM 只判 done。** `_extract_reply_text_from_dump`：dump → 按用户气泡 y 切割（`self._last_input_text` 定位）→ 过滤 chrome（`_REPLY_CHROME_LABELS` + streaming markers）→ 丢短 chip（有长节点时剔 <`MIN_CHIP_LEN`(25)）。scraped 比 VLM 长就 upgrade。`capture_full` scroll 阶段同样 scrape，失败才回落。
9. **权限弹窗自动 dismiss**：`predict` 入口 `_maybe_dismiss_permission_popup`。先 `dumpsys window` 拿前台包，不在 `_PERMISSION_PACKAGES` 白名单即 fast-exit。命中才 dump，按 `_ALLOW_LABELS`（始终允许>允许>Always allow>...）点 Allow，**永不 Deny**。每 task 上限 8 次。`RELAY_DISMISS_PERMISSIONS=0` 关。

### `x_capture_full_reply` 开不开？

口诀：**single TextView ⇒ 不开；RecyclerView 多节点 ⇒ 开**。判断：触发回复后 `adb shell uiautomator dump`，1 个长 TextView(>200字)→single-bubble；多个中等节点按卡片排→multi-node。

- **不开**（single-bubble：千问/WPS/携程 QA）：整段在一个 TextView，scrape 一次拿全；要全文调大 `max_seconds`。
- **开**（multi-node 卡片：order_food、高德 find_nearby、淘宝搜索、携程 search_*、微信 ai_search、XHS QA）：offscreen 卡片被回收须滚动。`max_scrolls`：短 4 / 标准 6 / 多日 8 / 深搜 15。
- **Skip**（短 CTA：高德 navigate_to、淘宝 buy_product、WPS ai_ppt、携程 plan_trip）。

**Scroll 幅度** `swipe_down(ratio=0.7)`，`RELAY_CAPTURE_SCROLL_RATIO` 覆写。大→省 VLM 但 seam 丢词；小→重叠多更稳。chunks 按捕获顺序拼接。

### `predict` 多次返回同一步

`wait_for_reply` 不推进 cursor（`_materialize` 返回 `advance=False`），runner 反复 predict 拿下一 poll。故 runner step 计数 ≠ plan cursor；thought 带 `[hold]` 表示 cursor 没推进；同一 step 号在 traj 多次出现正常。

## Trajectory 日志目录

每次 `mw test` 启动把上次 `traj_logs/user_task/` 搬到 `traj_logs/user_task_backup_<ts>/`（ts = 新跑启动时刻）。**本次输出永远在 `traj_logs/user_task/`**；`ls -td ...backup_* | head -1` 是**上一次**的内容。

## Handoff

最后一步调 `ask_user` 等终端输入。stdin 被重定向时以 `EOF when reading a line` 结束 —— 这是**成功**不是失败。
