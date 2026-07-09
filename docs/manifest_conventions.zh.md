<h1 align="center">Manifest 约定</h1>

<p align="center">
  <b>manifest 该怎么写：语言约定、prompt_template、x_capture_full_reply、卡片 swipe 方向、capability 关键字段</b>
</p>

<p align="center">
  <a href="manifest_conventions.md">English</a> | <b>中文</b>
</p>

> manifest 的规范字段定义见 [`SPEC.md`](../SPEC.md)；`prompt_template` 细节见 [`prompt_template.zh.md`](prompt_template.zh.md)。

---

## 🌐 1. 语言约定

**manifest 用对应 App 的语言写**：英文 App（如 `com.google.android.apps.bard`）的 manifest desc/注释用英文；中文 App（如 `com.autonavi.minimap`）用中文。`prompt_template`/`prompt_slots.desc` 同样按 App 目标 locale 写。

## 🧩 2. `prompt_template` —— 模板化 submit prompt

结构化能力（导航/订票/闹钟等）可声明 capability 级 `prompt_template`（+`prompt_slots`），把发给 in-app agent 的措辞固化，LLM 只抽槽位（`temperature=0`），避免措辞漂移让 App 端意图路由跑偏。

- **保证边界**：固定的是**措辞/意图路由**，槽位**取值**仍 LLM 抽（不保证取值正确）。
- required 缺槽**硬失败**；optional 槽用 `[ ... {slot} ... ]` 段包裹，缺值时整段（含周边措辞/空格/标点）删除。
- **加载期校验** `card_catalog._validate_prompt_template`：括号配对/非嵌套、占位符都是声明过的槽、声明的槽都被用到、required 槽不在 `[...]` 内、optional 槽必在 `[...]` 内——命中抛 `ManifestValidationError`（把作者笔误从运行期提前到加载期）。
- 仅作用于 NL flow（`run_plan.py`/`FlowPlanner`）。

完整规格、数据流、填充步骤、设计取舍见 [`prompt_template.zh.md`](prompt_template.zh.md)（English: [`prompt_template.md`](prompt_template.md)）。

## 📜 3. `x_capture_full_reply` 开不开？

口诀：**single TextView ⇒ 不开；RecyclerView 多节点 ⇒ 开**。判断法：触发回复后 `adb shell uiautomator dump`——1 个长 TextView(>200字)→single-bubble；多个中等节点按卡片排→multi-node。

- **不开**（single-bubble：千问 / WPS / 携程 QA）：整段在一个 TextView，scrape 一次拿全；要全文调大 `max_seconds`。
- **开**（multi-node 卡片：order_food / 高德 find_nearby / 携程 search_* / 微信 ai_search / XHS QA）：offscreen 卡片被回收须滚动。`max_scrolls`：短 4 / 标准 6 / 多日 8 / 深搜 15。
- **Skip**（短 CTA：高德 navigate_to / WPS ai_ppt / 携程 plan_trip）。

**Scroll 幅度** `swipe_down(ratio=0.5)`（clamp `[0.1, 0.5]`），`RELAY_CAPTURE_SCROLL_RATIO` 覆写（同样 clamp ≤0.5）。大→省 VLM 但 seam 丢词；小→重叠多更稳。chunks 按捕获顺序拼接。

## 👆 4. 卡片 `swipe` → scroll 动作（含方向反转）

manifest 里的 `swipe: <direction>` 按 **scroll / 内容移动方向** 写，**不按手指滑动方向**写。它经 `action_planner` 编成逻辑 `swipe` step，`_materialize` 发成 `scroll` 动作，`NativeEnv._dispatch` 对 up/down 做反转后再落底层 adb 手势：

- `swipe: up` → `scroll(direction="up")` → 内容上移/视觉向上滚 → 底层实际手势是**手指向下滑**。
- `swipe: down` → `scroll(direction="down")` → 内容下移/视觉向下滚 → 底层实际手势是**手指向上滑**。
- `left`/`right` 目前不反转。写卡片统一按 scroll 语义思考。

## 🔑 5. capability 关键字段（路由 / flow 相关）

| 字段 | 作用 |
| --- | --- |
| `handoff_to_user_required` | 把最终决定交回用户的 capability。非终态时 planner 必须后接 `ask_user`（见 [`nl_flow.zh.md`](nl_flow.zh.md) §2 Rule 4）。leg judge 对它改用 handoff 成功定义 |
| `x_skip_wait_for_reply` | 该 step 不抓文本回复——不能带 `bind`/`extract` |
| `executable` | 是否可运行（catalog/router 透传） |
| `example_prompts` | router stage-2 的 few-shot；无 `prompt_template` 时的兜底 |
| `prompt_template` / `prompt_slots` | 见上 §2 |

> **Source of truth**：cap × app 归属以 `docs/app_capability_matrix.csv` 为准，与 manifest/catalog 冲突时 **matrix 赢**；catalog 仅作 availability 校验剔除 stale pair。
