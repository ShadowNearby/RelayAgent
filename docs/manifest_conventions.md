<h1 align="center">Manifest Conventions</h1>

<p align="center">
  <b>How to author a manifest: language convention, prompt_template, x_capture_full_reply, card swipe direction, key capability fields</b>
</p>

<p align="center">
  <b>English</b> | <a href="manifest_conventions.zh.md">中文</a>
</p>

> The normative field definitions live in [`SPEC.md`](../SPEC.md); `prompt_template` details in [`prompt_template.md`](prompt_template.md).

---

## 🌐 1. Language convention

**Write a manifest in the app's own language**: an English app (e.g. `com.google.android.apps.bard`) gets English desc/comments; a Chinese app (e.g. `com.autonavi.minimap`) gets Chinese. `prompt_template`/`prompt_slots.desc` likewise follow the app's target locale.

## 🧩 2. `prompt_template` — templated submit prompt

Structured capabilities (navigation / ticketing / alarms / …) can declare a capability-level `prompt_template` (+`prompt_slots`) that fixes the wording sent to the in-app agent; the LLM only extracts slots (`temperature=0`), so phrasing drift can't throw off the app's intent routing.

- **What's guaranteed**: the **wording / intent routing** is fixed; the slot **values** are still LLM-extracted (value correctness is not guaranteed).
- A missing required slot **hard-fails**; an optional slot is wrapped in a `[ ... {slot} ... ]` segment, dropped whole (surrounding wording/spaces/punctuation included) when the value is absent.
- **Load-time validation** `card_catalog._validate_prompt_template`: balanced/non-nested brackets, every placeholder is a declared slot, every declared slot is used, required slots not inside `[...]`, optional slots only inside `[...]` — any hit raises `ManifestValidationError` (pulls authoring typos forward from runtime to load time).
- Applies to the NL flow only (`run_plan.py`/`FlowPlanner`).

Full spec, data flow, fill steps, and design trade-offs: [`prompt_template.md`](prompt_template.md) (中文: [`prompt_template.zh.md`](prompt_template.zh.md)).

## 📜 3. When to set `x_capture_full_reply`

Rule of thumb: **single TextView ⇒ off; multi-node RecyclerView ⇒ on**. To decide: after triggering a reply, `adb shell uiautomator dump` — one long TextView (>200 chars) → single-bubble; several medium nodes laid out as cards → multi-node.

- **Off** (single-bubble: Qwen / WPS / Ctrip QA): the whole answer is in one TextView, one scrape gets it all; bump `max_seconds` for the full text.
- **On** (multi-node cards: order_food / Amap find_nearby / Ctrip search_* / WeChat ai_search / XHS QA): offscreen cards are recycled and must be scrolled. `max_scrolls`: short 4 / standard 6 / multi-day 8 / deep search 15.
- **Skip** (short CTA: Amap navigate_to / WPS ai_ppt / Ctrip plan_trip).

**Scroll amount** `swipe_down(ratio=0.5)` (clamped to `[0.1, 0.5]`), overridable by `RELAY_CAPTURE_SCROLL_RATIO` (also clamped ≤0.5). Larger → fewer VLM calls but seam word-loss; smaller → more overlap, more robust. Chunks are concatenated in capture order.

## 👆 4. Card `swipe` → scroll action (with direction inversion)

A manifest's `swipe: <direction>` is written in terms of the **scroll / content-movement direction**, **not the finger-swipe direction**. It is compiled by `action_planner` into a logical `swipe` step, `_materialize` emits it as a `scroll` action, and `NativeEnv._dispatch` inverts up/down before issuing the low-level adb gesture:

- `swipe: up` → `scroll(direction="up")` → content moves up / scrolls up visually → the actual low-level gesture is a **finger swipe down**.
- `swipe: down` → `scroll(direction="down")` → content moves down / scrolls down visually → the actual low-level gesture is a **finger swipe up**.
- `left`/`right` are not inverted today. Author cards thinking in scroll semantics throughout.

## 🔑 5. Key capability fields (routing / flow related)

| Field | Effect |
| --- | --- |
| `handoff_to_user_required` | A capability that hands the final decision back to the user. When non-terminal the planner must follow it with `ask_user` (see [`nl_flow.md`](nl_flow.md) §2 Rule 4). The leg judge uses the handoff success definition for it |
| `x_skip_wait_for_reply` | The step captures no text reply — cannot carry `bind`/`extract` |
| `executable` | Whether it is runnable (passed through to catalog/router) |
| `example_prompts` | Few-shot for router stage-2; the fallback when no `prompt_template` is present |
| `prompt_template` / `prompt_slots` | See §2 above |

> **Source of truth**: cap × app membership is authoritative in `docs/app_capability_matrix.csv`; on any conflict with the manifest/catalog, **the matrix wins**; the catalog is only an availability check that drops stale pairs.
