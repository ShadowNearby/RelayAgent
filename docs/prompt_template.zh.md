# 模板化向 App Agent 提交的 prompt（`prompt_template`）

> English: [`prompt_template.md`](prompt_template.md)

> 一句话：发给 in-app agent 的 submit 措辞由**每能力一个模板**固定，LLM 只负责**抽槽位**，把"措辞漂移"这个难抓的失败面换成"抽槽"这个可校验的失败面。

---

## 1. 解决什么

原来发给 in-app agent 的 prompt 完全由 `FlowPlanner` 的 LLM **自由合成**（`_PLANNER_SYSTEM`），合成后再过 `_maybe_localize_prompt` 改语言。即"对 App agent 说什么话"的**措辞和取值都交给 LLM**。

问题：很多 in-app agent（尤其导航这类靠关键词触发 CTA 的能力）对措辞敏感——LLM 把"导航到 X"写成"帮我看看怎么去 X"就可能让 App 端从 `live_navigation` 掉到 `route_planning`。

模板化把**措辞固化**（如 Gemini 导航 = `Navigate to {place}.`），LLM 只抽 `place`。收益：

- 措辞 100% 确定，App 端意图路由不再受 LLM 措辞漂移影响；
- 风险从"措辞漂移"（难抓）挪到"抽槽"（**可校验**：required 槽缺失即报错）；
- 模板自带 locale，templated 步骤**跳过** `_maybe_localize_prompt` 那次 LLM 调用。

> **保证边界。** 模板固定的是**措辞与结构**，所以 App 端意图路由确定；它**不**保证**槽位取值**正确——取值仍由 LLM 抽取（`temperature=0` 降方差但不消除）。即保证的是"提交措辞 / 意图路由确定"，不是"提交内容正确"。

## 2. Manifest schema（核心 spec 字段）

挂在 capability 上，**无 `x_` 前缀**（已转正为核心 spec 字段）：

```yaml
- id: live_navigation
  description: >
    Gemini will open Google Map to Navigate to the place.
  prompt_template: "Navigate to {place}[ by {mode}]."   # 带 {slot} 的字面模板，按 App 目标 locale 书写
  prompt_slots:
    - name: place
      desc: "destination name or address"               # desc 也按 App locale 语言写
      required: true                                    # 默认 true
    - name: mode
      desc: "travel mode if the user specified one; omit otherwise"
      required: false                                   # 可选槽位
```

- `prompt_template`：带 `{slot}` 的字面模板，**已用 App 的目标 locale 写好**（en-US App 写英文模板，zh-CN App 写中文模板）。
- `prompt_slots`：`name` / `desc` / `required`（默认 `true`）。
- **无 `prompt_template` 的能力**（`foundation_llm`、`generate_slides` 等自由文本能力）走原自由合成路径，**完全不受影响**。

### 必填槽 vs 可选槽

- **必填槽**（`required: true`）：抽不出来 → **硬失败**（见 §4）。
- **可选槽**（`required: false`）：用户没提就 omit，不报错。要让"可选槽连同周边措辞一起省掉"，把它包进 `[...]` **可选段**：

  | 语法 | 含义 |
  | --- | --- |
  | `{slot}` | 替换为抽出的值（或上游 `{var}` token） |
  | `[ ... {slot} ... ]` | **可选段**：段内引用的每个已声明槽**都有非空值**才保留（去掉方括号、填充内部槽）；任一为空则**整段删除**（连周边文字/空格/标点一起）。不含已声明槽的 `[...]` 当字面文本原样保留 |

  例：`Navigate to {place}[ by {mode}].`
  - 没给出行方式 → `Navigate to Hong Kong International Airport.`（与无可选段的旧模板**字节一致**，向后兼容）
  - "开车导航…" → `Navigate to Hong Kong International Airport by driving.`

  > 把可选槽的周边措辞（含空格/标点）都放进方括号内；裸用（不包 `[...]`）的可选槽缺值时只会被替成空串、留下空档。段删后残留的多余空格会自动归一。

样板：Gemini `live_navigation` = `Navigate to {place}[ by {mode}].`（必填 `place` + 可选 `mode`）；高德 `live_navigation` = `导航去{place}`。其余结构化能力（订餐/订票/闹钟）后续按同 pattern 增量加，无需改代码。

## 3. 落地链路（`agents/flow/flow_planner.py`）

数据流：`FlowPlanner.plan()` LLM 合成自由 prompt → `resolve_app_routes()` 逐 step 路由出 app+capability → **填充** → `step["prompt"]` 经 `render()` 替换 `{var}` 后作为 `RELAY_INVOCATION_TEXT` 传子进程。

**填充插入点 = `_route_one_step()`**（路由定下 capability 后，此刻才知道挂哪个模板）：

- capability 有 `prompt_template` → 调 `_fill_prompt_template()`，**跳过** `_maybe_localize_prompt`；
- 否则维持原自由合成 + localize。

`_fill_prompt_template()` 步骤：

1. 把**上游已 bind 的变量**（`produced`，按 plan 顺序累计）作为"可引用的上游变量"白名单传给抽槽 LLM——和第 5 条的 `{var}` 守卫**同一集合**，所以合规的抽槽输出绝不会被守卫反手拒掉。
2. 一次小 LLM 调用（`_SLOT_EXTRACT_SYSTEM`，`temperature=0`）：输入模板、slot 规格、`nl_request`、合成 prompt、可引用上游 `{var}`；输出 fenced JSON `{"slots": {...}, "missing": [...]}`。值优先用 NL 原词；若该槽对应上游某步产物，返回 `{var}` token 原样。
3. **缺槽硬失败**：任一 `required` 槽缺失 / 在 `missing` 里 → 抛 `PromptTemplateError`，并入 `resolve_app_routes` 的 `errors`、统一 `raise PlanValidationError`。**绝不把残缺 prompt 提交给 App agent。**（"缺失"判定与可选段共用 `_has_slot_value`：值为 `None` 或空白才算缺，数值 `0` 算有值。）
4. `_fill_template()`：只对**已声明 slot 名**做 `{name}`→值 的定向替换（不用 `str.format`，避免误伤跨步 `{var}`）。
5. **`{var}` 守卫**（复用 localize 同款）：填充结果里出现的 `{var}` 必须 ⊆ `produced`（上游已 bind 的变量），越界则抛错。

### 语法为何不冲突

模板槽 `{place}` 与跨步 `{var}` 都被 `flow_runner._VAR_RE` 匹配，但**填充在 plan 期、`render()` 在 runtime 期**：填充把 `{place}` 替换成字面值（`Navigate to Hong Kong International Airport.`）或上游 `{var}` token（`Navigate to {poi.name}.`），plan 期结束已无 `{slot}` 残留，runtime `render()` 只处理跨步 `{var}`。

### catalog 透传

`build_catalog`（`agents/routing/card_catalog.py`）默认裁剪 capability 字段。`prompt_template` / `prompt_slots` **仅在存在时**透传进 catalog digest，让 `FlowPlanner._caps` 取得到。

**加载期校验。** 构建 catalog 时 `_validate_prompt_template` 逐个检查 templated 能力，命中即抛 `ManifestValidationError`（fail-fast）：占位符 `{}` 未声明（如拼错 `{palce}`）、声明了却没用到的死槽、**required** 槽只出现在 `[...]` 段内（会被 drop）、**optional** 槽没包进 `[...]`（会留空隙）、或括号不配对/嵌套。把作者笔误从"该 step 跑到时才 `PromptTemplateError`"提前到加载期。

## 4. 设计决策

| 决策 | 取值 | 理由 |
| --- | --- | --- |
| 缺 required 槽 | **硬失败** | 宁可不跑，也不把残缺/瞎编 prompt 提交给 App agent；保证提交措辞/意图路由确定（槽位**取值**仍由 LLM 抽，故保证的是意图路由、非取值正确） |
| v1 覆盖范围 | **仅 NL flow**（`run_plan.py`/`FlowPlanner`） | prompt 被自由合成、最易漂移的路径；直连 `python -m agents.runtime.native_runner <pkg> <goal>` 用用户原话，暂不模板化 |
| planner system prompt | **不改** | planner 此时尚不知路由结果，无法知道哪条套模板；抽槽器从合成 prompt 取值即可，零 planner 改动、风险最低 |
| `example_prompts` | **保留** | 作为模板缺失时的 few-shot 回退 |

## 5. TODO

- **嵌套可选段**：`_OPT_SEGMENT_RE` 只匹配非嵌套 `[...]`，暂不支持 `[a[b]]`。当前需求用平铺段已够。
- 把模板机制提案进 card spec 正式版后，考虑 bump 各 manifest 的 `spec_version`。

## 6. 验证

```bash
# 确定串（无 LLM 措辞抖动）
uv run python scripts/run_plan.py --dry-run "用 Gemini 导航到香港国际机场"
#   1. [agent] Gemini/live_navigation -> Navigate to Hong Kong International Airport.

# 真机冒烟
uv run python scripts/run_plan.py --yes "用 Gemini 导航到香港国际机场"
```

无模板能力（如千问 `foundation_llm`）跑 `--dry-run` 应仍走自由合成 + localize，行为不变。
