# AppAgentCards — Claude 项目记忆

## Python 环境

- venv 在 `.venv/`，**Python 3.12.3**（MobileWorld 要求 `>=3.12,<3.13`，3.11 不行）。
- 日常装依赖（**不**把本项目装进 venv，靠 `uv run` 直接跑源码）：

  ```bash
  uv venv --python 3.12
  uv sync --no-install-project
  ```

- 如果要把项目本体也装进去（`uv pip install .`），`pyproject.toml` 里已经写好
  `[tool.hatch.build.targets.wheel] packages = ["agents"]`，hatchling 能直接
  打 wheel —— 不需要再像早期那样手工补 packages 字段。

- MobileWorld 通过 `pyproject.toml` 里的 git 依赖 + `[tool.uv.sources]` 由 uv
  自动 clone 安装（详见下面 "MobileWorld 依赖" 一节），`mw` / `mobile-world` 可用。
- **历史坑（已通过 pyproject 固定，不需再手动处理）**：`fastmcp 2.9.2` 与 pydantic
  `>=2.11` 不兼容（`default` + `default_factory` 同时存在 → `TypeError`）。
  `pyproject.toml` 里写了 `"pydantic<2.11"`，当前锁在 `pydantic==2.10.6` /
  `pydantic-core==2.27.2`。如果未来升级 fastmcp 后这个限制可以放开。

## 用户的 LLM 端点

具体值写在 `.env`（gitignore，**不要提交，也不要在回复里复述完整 key**）：

| 变量 | 含义 |
| --- | --- |
| `LLM_BASE_URL` | `http://yjs-ipads.ipads-lab.se.sjtu.edu.cn:3000/v1` —— SJTU IPADS 实验室的 OpenAI 兼容网关 |
| `LLM_API_KEY` | `sk-PqO0...` —— 仅从 `.env` 读取，勿在回复/提交中写出 |
| `LLM_MODEL` | `qwen` |

**首选入口（canonical）**：`scripts/run_test.py`。它会自己 load `.env`、冷启动目标
app、设 `APPCARDS_SKIP_OPEN_APP=1`，然后转发剩余 flag 给 `mw test`：

```bash
uv run python scripts/run_test.py com.aliyun.tongyi "帮我点三杯蜜雪冰城蜜桃四季春"
# 多 app NL 路由：
uv run python scripts/run_nl.py "在北京找三家独立书店，挑一家打车过去"
# 多 app flow：
uv run python scripts/run_flow.py manifests/_flows/xhs_to_amap_place.yaml --nl "..."
```

**直接 `mw test`（只在调试 adapter 时用）**：

```bash
set -a; source .env; set +a
export APPCARDS_TARGET_APP=com.aliyun.tongyi   # 目标 app 包名，必填

uv run mw test "帮我点三杯蜜雪冰城蜜桃四季春" \
    --agent-type   "$PWD/agents/appcards_agent.py" \
    --model_name   "$LLM_MODEL" \
    --llm_base_url "$LLM_BASE_URL" \
    --api_key      "$LLM_API_KEY"
```

参数名必须与 `agents/appcards_agent.py` 中 `AppCardsAgent.__init__` 签名一致：
`model_name` / `llm_base_url` / `api_key`，**不是** `--base_url`。绕过
`scripts/run_test.py` 直接走 `mw test` 时调用方没冷启动，planner 会自己发出
`open_app` 步（见 §3.5）。

## MobileWorld 依赖

`mw` 来自外部仓库 [Tongyi-MAI/MobileWorld](https://github.com/Tongyi-MAI/MobileWorld)，已经通过
`pyproject.toml` 的 `[tool.uv.sources]` 声明为 git 依赖（pin 到某个 commit）。
`uv sync` 会自动 clone+安装到 venv，不需要手动 `git clone`。

```bash
uv sync --no-install-project    # 装本项目依赖（含 mobile-world）
uv run mobile-world server &    # 启动 MW server
```

升级 MobileWorld：编辑 `pyproject.toml` 里 `mobile-world` 那条的 `rev` 然后
重新 `uv sync` 即可。pydantic 的 `<2.11` 上限同样写在 `pyproject.toml`，不会被
`uv run` 自动 bump 回去（CLAUDE.md 顶部那段历史背景仍适用）。

需要 adb + 真机 USB 调试 + `com.android.adbkeyboard/.AdbIME`。

## Adapter 关键设计点（`agents/appcards_agent.py` + `agents/action_planner.py`）

### 已修的坑

1. **`open_app` 要 launcher label，不是包名。** MobileWorld 的 `open_app` 期望桌面图标显示名（通义=`千问`），不是 `com.aliyun.tongyi`。修复：优先用 `card.embedded_agent.name`，再回退 `card.app_name`，最后才包名。

2. **Grounding LLM 输出形态比 spec 宽。** Prompt 让模型返回 `{"x":int,"y":int}`，但 Qwen-VL 实际返回 `[{"x":[446,920]}]`（数组、`x` 字段塞 `[x,y]`、没 `y`）。`_extract_xy()` 容忍多种形态：`{x,y}` / `{point:[x,y]}` / `[{x:[x,y]}]` / `{bbox:[x1,y1,x2,y2]}` / 纯数字回退。
   - 隐患：Qwen-VL 返回的 `[446, 920]` 不一定真是 0–999 归一坐标（Qwen 默认输出可能是基于 resize 后的像素）。`_ground_text` 用启发式：若 `rx > 999 或 ry > 999` 当像素，否则当归一乘 `screen/999`。出现系统性偏移再调。

3. **`tap_text` 优先 uiautomator XML，VLM 是兜底。** 任何文本/语义选择器先走 `_ground_text_via_uiautomator`（`adb shell uiautomator dump` → 解析 XML → 按 text / content-desc / resource-id 匹配 → bounds 中心）。VLM 在中文移动 UI 上准确率很差（曾把"新建对话"定位到 (2, 965)），uiautomator 几乎 100% 命中。
   - 动画延迟：tap_text 内置最多 **3 次重试 + 0.8s 间隔** dump，吃掉抽屉打开等动画。
   - 沉默失败禁忌：早期所有失败路径都是 `logger.debug`，被默认日志级别吞掉，看着像"功能不工作"实际上是 dump 出错。已全部升级到 `logger.info/warning`。
   - 局限：通义的输入框 placeholder TextView `clickable=false`，scoring 会给低分，但只要 `text` 字段精确匹配仍然命中并返回 bounds 中心；点击事件穿透到真正的 EditText 兄弟节点上。

3.5. **App 冷启动由调用方负责，planner 默认会包含 `open_app`，但可关闭。** `scripts/run_test.py` / `scripts/run_nl.py` / `agents/flow_runner.py` 在调 `mw test` 之前用共享 helper `agents/_adb.py:cold_launch()`（force-stop + monkey LAUNCHER + settle）自己拉起目标 app，然后给子进程设 `APPCARDS_SKIP_OPEN_APP=1`；planner 看到这个环境变量就跳过最开头的 `open_app + 2.5s wait`。这样首张截图直接是 app 主页，无需依赖 MobileWorld 的 `open_app` 实现。如果绕过脚本直接 `mw test`，planner 仍会发出 `open_app` 步骤；`_materialize` 在 open_app 分支里会先调 `agents/_adb.py:force_stop()` 再让 MobileWorld 走 launcher tap。这四个调用点（三个脚本入口 + adapter 内的 open_app 分支）共用 `agents/_adb.py` 同一实现，并都支持 `APPCARDS_ANDROID_SERIAL` 选设备。

4. **`x_prepare_fresh_conversation` 一定要接进 planner。** SPEC 用 `x_` 前缀表示非标扩展字段，老代码的 `build_plan()` 没读它，每次跑都带着上次的历史上下文。现在 `build_plan(... , fresh_conversation=True)` 默认在 `open_app + cold-launch wait` 之后插这段步骤。可用环境变量 `APPCARDS_FRESH_CONV=0` 关掉。

5. **`wait_for_reply` 用 VLM 轮询而不是死等 `typical_latency_seconds`。** 系统 prompt 见 `_REPLY_WATCH_SYSTEM`，VLM 同时回 `{done, text}`。
   - **`done=True && text==None` 视为不可信**：VLM 自己都没读到文字，几乎肯定还没生成完。强行 distrust 继续 poll，比把弹窗误判成"回复"安全。
   - 超时按 **墙钟秒** 计：`max_seconds = capability.x_max_wait_seconds or max(5×typical_latency, 60)`，用 `time.monotonic()` 比较 elapsed。每次 poll 本身是一次 VLM 调用（数秒），墙钟语义和"实际等了多久"对得上；早期版本按 poll 次数算的语义已经废弃。日志里会同时打 `poll N @ X.Xs/Ys`。**历史坑**：以前是 `max(3×latency, 30)`，对 single-bubble chat（千问/WPS chat lat=8-10s）来说 max_seconds=30 太紧 —— 多段落回复 30s 还没流完就 timeout，导致 scrape 抓到的是半截。改成 5× / 60 floor 后千问 chat 拿到 60s，长回复也能完整 stream 完。Per-cap 覆写写 `x_max_wait_seconds: 120` 之类。
   - 抓到的 `text` 会注入到 handoff 的 `ask_user` 消息里给用户看，不是只用来判 done。

6. **`wait_for_reply` 用两段式 precheck 省 VLM 调用。** 每次 poll 前：
   - **Stage 1（≈25 ms / tick）**：`_hash_screenshot_region` 对 MW 已经传进来的 PIL 截图做裁剪 + 灰度 + 48×96 下采样 + blake2b。裁掉顶部 8% 状态栏（不让时钟扰动 hash）和底部 18% 输入区。hash 跟上一 tick 不同 → 还在 streaming → 跳过 stage 2 和 VLM（日志 `precheck skip #N (screen changed)`）。
   - **Stage 2（≈2.5 s / tick，只在屏幕稳定的那一拍才跑）**：`_dump_visible_text_hash` 对 uiautomator dump 里所有可见文本（`text` + `content-desc`）按文档顺序拼接后做 blake2b。**比较本 tick 和上 tick 的 dump 文本 hash**：
     - 没 baseline（首次 dump）→ fall through 到 VLM
     - hash 跟上次不同 → 文字还在长 → skip VLM（日志 `precheck skip #N (text still growing)`）
     - hash 跟上次相同（连续两次 dump 文本完全相等）→ 真的稳定了 → 调 VLM 判 done
   - **设计原则**：text-hash diff 是 app-agnostic 的语义信号（直接测"reply 还在长不"），不依赖 per-app 的 marker 列表。**历史坑**：早期 Stage 2 是扫 `停止生成 / Stop generating` 这类 marker —— 脆弱，理由：(a) 不是每个 app 都有 stop 按钮；(b) 有些 app 的 stop 按钮生成完也不消失（粘连 false-negative）。改成 text-diff 后两类都正确处理。`_DEFAULT_STREAMING_MARKERS` 仍保留，但仅作为 reply text scrape 的 chrome filter（防止 "停止生成" 字样混入提取的回复文本）。
   - stage 2 dump 超时 3s/pull 2s（短于通用 8s/5s），避免 uiautomator 卡死时烧光 wall-clock budget。
   - **熔断**：同一次 wait 内连续 ≥2 次 stage-2 dump 失败 → 关掉本次的 dump，稳定的屏直接走 VLM。stage 1 截图 hash 永远开。
   - **看门狗**：连续 precheck-skip ≥5 次 → 强制跑一次 VLM。防某些 app 的动画一直翻 screenshot hash 或某种 chrome 文字一直微动让 text hash 一直变。
   - 实测千问 chat 简短回复（清华介绍）：3 次 VLM poll 砍到 1 次 + 3 次 stage-1 skip，**~40% token 节省**（6740 → 3950），且没有一次 stage-2 dump 真的跑（屏从 streaming 直接稳定到 done）。回复越长，stage 1 省得越多；只有屏稳定那一拍才付 dump 成本。

7. **系统权限弹窗自动 dismiss。** 每次 `predict` 入口先跑 `_maybe_dismiss_permission_popup`：
   - 先用 `adb shell dumpsys window` 拿前台包（~130ms），不在 `_PERMISSION_PACKAGES` 白名单里直接 fast-exit，不付 uiautomator dump。
   - 白名单命中才 dump XML，按 `_ALLOW_LABELS` 优先级（`始终允许 > 允许 > Always allow > Allow > ...`）找第一个 clickable 节点，`adb shell input tap` 中心点。
   - 每个 task 上限 8 次 dismiss（`MAX_DISMISSALS`），防止卡死的对话框无限循环。
   - 只点 Allow，永远不点 Deny；deny-only 对话框正常跳过。
   - 关掉：`APPCARDS_DISMISS_PERMISSIONS=0`。
   - 替代了之前"手动在系统设置预先授权"的临时解法。

8. **回复文本优先从 uiautomator 抓，VLM 只判 done。** `_extract_reply_text_from_dump` 走 dump → 按 y 坐标筛"用户气泡之下"的文本节点（用 `self._last_input_text` 在 input_text step 时保存的字符串定位用户气泡）→ 过滤 chrome 标签（`_REPLY_CHROME_LABELS` + streaming markers）→ 启发式丢"快速回复 chip"（有长节点存在时，剔除 <25 chars 的节点；无长节点则全保留以兼容短回复）。
   - **happy path**（VLM 判 done && text 不空）：抓一次 dump，如果 scraped 比 VLM text 更长就 upgrade，日志 `reply text upgrade: VLM=X chars → uiautomator scrape=Y chars`。
   - **timeout path**（max_seconds 到了 VLM 还说 not done）：同样 upgrade。实测千问 chat "详细介绍十种损失函数" 这种回复：VLM text 卡在 ~120 chars（500-char cap + JSON 包裹），scrape 拿回 1732 chars，**约 14× 内容恢复**，零额外 VLM 调用，只多一次 ~2.5s dump。
   - **`capture_full` scroll 阶段**：每次 scroll 后用 scrape 替代 VLM 提取当帧文本。dedup / stitch 逻辑不变（normalize 后比较，超集替换）。scrape 失败（如 WebView 渲染的回复 a11y 抓不到）才回落到 VLM，日志区分 `via scrape` / `via vlm_fallback`。
   - 启发式不依赖 per-card 配置（chip-filter 25-char 阈值 + chrome labels 静态表 + user-bubble y 切割），但 `_REPLY_CHROME_LABELS` 和 `MIN_CHIP_LEN` 是常量，要补的话直接改源。后续如果某些 app（高德 POI 卡片、携程行程卡）需要保留 clickable 子节点，再加 `x_reply_scrape: {keep_clickable: true, exclude: [...]}` per-card 覆写。

### `x_capture_full_reply` 该不该开？

判断口诀：**回复是 single TextView ⇒ 不开；是 RecyclerView 多节点 ⇒ 开**。

- **不开（single-bubble chat）**：千问 chat / WPS chat / WPS 长文写作 / WPS 文档阅读 / WPS 网页摘要 / 携程 chat_travel_qa / 携程 search_attraction_info。这类 app 把整段回复（哪怕几千字）渲染在**一个 TextView 节点**里，Android 的 uiautomator 一次 dump 拿的是节点 `text` 属性的**完整字符串**，跟可视裁剪无关 — scrape 一次就拿全。开 capture_full 反而会把后续 CTA（复制、handoff 按钮）滚出视口。这类需要拿全回复的是**调大 `max_seconds`**（已通过 5× / 60 默认覆盖），不是 capture_full。
- **开（multi-node card list）**：千问 order_food / book_*、高德 find_nearby / plan_trip / hail_ride、淘宝 search_product / compare_products / track_order、携程 search_flight / search_hotel / search_train、微信 ai_search、小红书 qa_community_knowledge。这类 app 用 RecyclerView/ListView 渲染卡片列表，offscreen 卡片会被回收 → 不滚动 dump 不到。`max_scrolls` 按内容长度配：短列表 4，标准 6，多日行程 8，深度搜索 15（微信 ai_search 的经验值）。
- **Skip（短 CTA / handoff 前奏）**：高德 navigate_to、淘宝 buy_product / order_local_delivery、千问 hail_ride（短确认对话）、WPS ai_ppt（outline + CTA）、携程 plan_trip（短 prompt + CTA，行程在 handoff 后）。

要判断一个新 capability 属于哪类，最快办法是触发一次 reply，然后跑 `adb shell uiautomator dump` 看长文本节点数：1 个长 TextView（>200 字）→ single-bubble；多个中等节点（每个几十字，按卡片排）→ multi-node list。

### `predict` 多次返回同一 plan 步的语义

`wait_for_reply` 是**不推进 cursor 的 step**——`_materialize` 返回 `(action, advance=False, note)`，runner 下一次还会调一次 predict 拿到下一个 poll。`wait_for_reply` 的 `capture_full` 分支里 scroll 阶段同样保持 advance=False。这导致：

- runner 的 step 计数 ≠ agent 的 plan cursor。例如 plan 第 8 步（wait_for_reply）会占多个 runner step。
- `predict` 的 thought 字符串是 1-based **当前**步号：`step 8/13: wait_for_reply (...)`。当 step 还没推进 cursor（poll 还在继续）时会带 `[hold]` 后缀，例如 `step 8/13: wait_for_reply (... poll 3 @ 12.4s/45s) [hold]`。同一 step 号在 traj 里出现多次是**正常**的。

## Trajectory 日志的目录约定（容易看错）

每次 `mw test` 启动时，MobileWorld 把上一次的 `traj_logs/user_task/` 整个搬到 `traj_logs/user_task_backup_<时间戳>/` 然后开始新跑。

- **本次跑的活动输出永远在 `traj_logs/user_task/`**（`traj.json` / `screenshots/` / `thread_*.log`）。
- `user_task_backup_<ts>/` 是**上一次**的快照，时间戳是新跑启动的时刻，**不是**那次跑的时间。
- 用 `ls -td traj_logs/user_task_backup_* | head -1` 拿"最新备份"得到的是**这次跑之前**那一次的内容，不要把它当成本次跑的输出来调试。

## Handoff 行为

跑到最后一步会调用 `ask_user`（`handoff_to_user_required: true` 的能力一定会这样），等用户在终端输入确认。如果 stdin 被重定向（`tail` / 管道 / `< /dev/null`）会以 `EOF when reading a line` 结束 —— 这是**成功**而非失败。

## VLM 调用次数预算（每个任务）

理想路径下：

| 来源 | 次数 |
| --- | --- |
| capability 路由（纯文本） | 1 |
| `tap_text` → uiautomator 命中 | 0 |
| `tap_text` → uiautomator miss 才走 VLM grounding | 0–N |
| `wait_for_reply` done detection（**precheck 后**） | 1–N（典型 1–2）|
| `wait_for_reply` 提取回复 text | **0**（scrape；失败才 VLM）|
| `capture_full` scroll 阶段抓 chunk | **0/scroll**（scrape；失败才 VLM）|

第 6 条的两段式 precheck 砍 done 检测的 VLM 次数（~30–50%）；第 8 条的 scrape upgrade 砍 text 提取的 VLM token 成本（capture_full 多帧场景从 N 次 VLM 直接到 0）。两条合起来：千问 chat 短回复 6740 → 4040 token；长回复（损失函数那种）VLM token 不变但**回复内容 ~14× 恢复**（绕开 500-char cap）。蜜雪冰城下单链路启用两条优化后预期降到 ~3500–4000 数量级。

### Capture-scroll 幅度可调

`wait_for_reply` 的 `capture_full` 阶段不再用 MW 的固定 0.4×width 小幅度
swipe。`agents/_adb.py:swipe_down(ratio=0.7)` 直接发 `input swipe`，幅度 =
`ratio × wm size height`（clamp 0.1–0.95）。环境变量
`APPCARDS_CAPTURE_SCROLL_RATIO` 覆写默认值。

- 调大（0.8–0.9）→ 砍 VLM poll 次数，但相邻帧重叠少，seam 处可能丢词。
- 调小（0.4–0.5）→ 重叠多更稳，但 VLM 调用更多。
- 方向：finger 从底往上推，把当前可见内容推出视口，露出**下方**的新内容（即向后读）。XHS 点点等场景下回复从上往下渲染，可见的是开头，后续内容在视口下方 — 所以 capture 阶段是顺序走读，chunks 直接按捕获顺序拼接，不再 `reversed()`。

## 已知阻塞

（目前无 — 历史阻塞"千问相机权限弹窗"已由第 7 条的 `_maybe_dismiss_permission_popup` hook 解决。）
