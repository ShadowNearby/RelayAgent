# AppAgentCards — Claude 项目记忆

## Python 环境

- venv 在 `.venv/`，**Python 3.12.3**（MobileWorld 要求 `>=3.12,<3.13`，3.11 不行）。
- 因为 `pyproject.toml` 没有 `appagentcards/` 源码目录，hatchling editable 构建会失败，所以用：

  ```bash
  uv venv --python 3.12
  uv sync --no-install-project
  ```

  装项目本体之前需先在 `pyproject.toml` 加 `[tool.hatch.build.targets.wheel] packages = [...]`。

- MobileWorld 已从 `~/MobileWorld` 安装进本 venv（`mw` / `mobile-world` 可用）。
- **已知坑**：`fastmcp 2.9.2` 与新版 pydantic 不兼容（`default` + `default_factory` 同时存在会抛 `TypeError`）。安装 MobileWorld 后必须降级：

  ```bash
  VIRTUAL_ENV=$PWD/.venv uv pip install "pydantic<2.11"
  ```

  当前固定在 `pydantic==2.10.6` / `pydantic-core==2.27.2`。

## 用户的 LLM 端点（运行 `mw test` 时的默认配置）

具体值写在 `.env`（gitignore，**不要提交，也不要在回复里复述完整 key**）：

| 变量 | 含义 |
| --- | --- |
| `LLM_BASE_URL` | `http://yjs-ipads.ipads-lab.se.sjtu.edu.cn:3000/v1` —— SJTU IPADS 实验室的 OpenAI 兼容网关 |
| `LLM_API_KEY` | `sk-PqO0...` —— 仅从 `.env` 读取，勿在回复/提交中写出 |
| `LLM_MODEL` | `qwen` |

运行模板：

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
`model_name` / `llm_base_url` / `api_key`，**不是** `--base_url`。

## MobileWorld 依赖

`mw` 来自外部仓库 [Tongyi-MAI/MobileWorld](https://github.com/Tongyi-MAI/MobileWorld)，需先：

```bash
git clone https://github.com/Tongyi-MAI/MobileWorld && cd MobileWorld
uv pip install . --python /home/yjs/AppAgentCards/.venv/bin/python
uv run --python /home/yjs/AppAgentCards/.venv/bin/python mobile-world server &
```

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

4. **`x_prepare_fresh_conversation` 一定要接进 planner。** SPEC 用 `x_` 前缀表示非标扩展字段，老代码的 `build_plan()` 没读它，每次跑都带着上次的历史上下文。现在 `build_plan(... , fresh_conversation=True)` 默认在 `open_app + cold-launch wait` 之后插这段步骤。可用环境变量 `APPCARDS_FRESH_CONV=0` 关掉。

5. **`wait_for_reply` 用 VLM 轮询而不是死等 `typical_latency_seconds`。** 系统 prompt 见 `_REPLY_WATCH_SYSTEM`，VLM 同时回 `{done, text}`。
   - **`done=True && text==None` 视为不可信**：VLM 自己都没读到文字，几乎肯定还没生成完。强行 distrust 继续 poll，比把弹窗误判成"回复"安全。
   - 最长 `max(3×typical_latency, 30)` 秒（poll 间隔 ~1s，由 MobileWorld step_wait_time 控制，不是我们的代码）。
   - 抓到的 `text` 会注入到 handoff 的 `ask_user` 消息里给用户看，不是只用来判 done。

### `predict` 多次返回同一 plan 步的语义

`wait_for_reply` 是**唯一一个不推进 cursor 的 step**——`_materialize` 返回 `(action, advance=False, note)`，runner 下一次还会调一次 predict 拿到下一个 poll。这导致：

- runner 的 step 计数 ≠ agent 的 plan cursor。例如 plan 第 8 步（wait_for_reply）会占多个 runner step。
- `traj.json` 里看到的 prediction 字符串可能写"step 8/13"两次，第一次是 submit click，第二次是 wait poll —— 这是**正常**的，不是 bug。

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
| `wait_for_reply` poll | 1–N（典型 2–5）|

蜜雪冰城下单一条完整链路当前 token_usage 约 6800 total，主要在 wait_for_reply 的多张截图轮询。

## 已知阻塞

- **千问首次触发外卖类能力会弹相机权限**（`要允许"千问"拍摄照片和录制视频吗？`）。我们没加权限弹窗处理。临时解法：手动在系统设置里把千问的相机权限预先授掉。后续如果要做，可以在 `_materialize` 里加一个 step 之前的 "如检测到系统权限弹窗就点允许" 钩子。
