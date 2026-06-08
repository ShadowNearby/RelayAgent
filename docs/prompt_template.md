# Templated submit prompt for the in-app agent (`prompt_template`)

> 中文版：[`prompt_template.zh.md`](prompt_template.zh.md)

> In one line: the wording submitted to an in-app agent is fixed by a
> **per-capability template**; the LLM only **extracts slots**. This swaps the
> hard-to-catch "phrasing drift" failure surface for the verifiable
> "slot extraction" one.

---

## 1. What it solves

The prompt sent to an in-app agent used to be **freely synthesized** by
`FlowPlanner`'s LLM (`_PLANNER_SYSTEM`), then run through
`_maybe_localize_prompt` to fix the language. So both the **wording and the
values** of "what to say to the app agent" were left to the LLM.

Problem: many in-app agents — navigation especially, where a CTA is triggered by
keyword matching — are phrasing-sensitive. If the LLM turns "navigate to X" into
"help me figure out how to get to X", the app side can fall from
`live_navigation` down to `route_planning`.

Templating **fixes the wording** (e.g. Gemini navigation = `Navigate to {place}.`)
and lets the LLM extract only `place`. Benefits:

- Wording is 100% deterministic — the app's intent routing is no longer subject
  to LLM phrasing drift;
- The risk moves from "phrasing drift" (hard to catch) to "slot extraction"
  (**verifiable**: a missing required slot is an error);
- The template carries its own locale, so a templated step **skips** the
  `_maybe_localize_prompt` LLM call.

> **What is (and isn't) guaranteed.** The template fixes the *wording and
> structure*, so the app's intent routing is deterministic. It does **not**
> guarantee the *slot values* are correct — those are still LLM-extracted (at
> `temperature=0`, which lowers variance but doesn't eliminate it). So the
> guarantee is "submitted wording / intent routing is determined", not
> "submitted content is correct".

## 2. Manifest schema (core spec fields)

Attached to a capability, **no `x_` prefix** (promoted to core spec fields):

```yaml
- id: live_navigation
  description: >
    Gemini will open Google Map to Navigate to the place.
  prompt_template: "Navigate to {place}[ by {mode}]."   # literal template with {slot}, authored in the app's target locale
  prompt_slots:
    - name: place
      desc: "destination name or address"               # desc also written in the app's locale language
      required: true                                    # defaults to true
    - name: mode
      desc: "travel mode if the user specified one; omit otherwise"
      required: false                                   # optional slot
```

- `prompt_template`: literal `{slot}` template, **already written in the app's
  target locale** (en-US app → English template, zh-CN app → Chinese template).
- `prompt_slots`: `name` / `desc` / `required` (defaults to `true`).
- Capabilities **without** `prompt_template` (`foundation_llm`,
  `generate_slides`, and other free-form ones) keep the original free-synthesis
  path — **entirely unaffected**.

### Required vs optional slots

- **Required** (`required: true`): cannot be extracted → **hard fail** (see §4).
- **Optional** (`required: false`): omitted when the user didn't express it, no
  error. To drop the slot *together with its surrounding wording*, wrap it in a
  `[...]` **optional segment**:

  | Syntax | Meaning |
  | --- | --- |
  | `{slot}` | replaced by the extracted value (or an upstream `{var}` token) |
  | `[ ... {slot} ... ]` | **optional segment**: kept (brackets stripped, inner slots filled) only when every declared slot it references has a non-empty value; otherwise the whole segment is dropped (surrounding text/spaces/punctuation included). A `[...]` with no declared slot inside is left as literal text |

  Example: `Navigate to {place}[ by {mode}].`
  - no travel mode → `Navigate to Hong Kong International Airport.` (**byte-identical**
    to the no-optional-segment template — backward compatible)
  - "navigate by car…" → `Navigate to Hong Kong International Airport by driving.`

  > Put an optional slot's surrounding wording (incl. spaces/punctuation) inside
  > the brackets; a bare (un-bracketed) optional slot with no value is just
  > stripped to '', leaving a gap. Double spaces left by a dropped mid-sentence
  > segment are collapsed automatically.

Seeds: Gemini `live_navigation` = `Navigate to {place}[ by {mode}].` (required
`place` + optional `mode`); Amap `live_navigation` = `导航去{place}`. Other
structured capabilities (food ordering / ticketing / alarms) can be added
incrementally with the same pattern, no code change needed.

## 3. Where it plugs in (`agents/flow_planner.py`)

Data flow: `FlowPlanner.plan()` LLM synthesizes a free prompt →
`resolve_app_routes()` routes each step to app+capability → **fill** →
`step["prompt"]` is substituted by `render()` for `{var}` and passed as
`RELAY_INVOCATION_TEXT` to the subprocess.

**Fill point = `_route_one_step()`** (only after routing is the capability — and
thus which template applies — known):

- capability has `prompt_template` → call `_fill_prompt_template()`, **skip**
  `_maybe_localize_prompt`;
- otherwise keep the original free-synthesis + localize.

`_fill_prompt_template()` steps:

1. Pass the **upstream-produced variables** (`produced`, accumulated in plan
   order) as the allow-list of "referencable upstream vars" for the slot
   extractor — the **same set** the step-5 `{var}` guard validates against, so a
   compliant extraction can never be rejected by the guard afterward.
2. One small LLM call (`_SLOT_EXTRACT_SYSTEM`, `temperature=0`): inputs are the
   template, slot specs, `nl_request`, the synthesized prompt, and the
   referencable upstream `{var}`; output is fenced JSON
   `{"slots": {...}, "missing": [...]}`. Values prefer the user's own NL wording;
   if a slot maps to an upstream step's result, the `{var}` token is returned
   verbatim.
3. **Missing-slot hard fail**: any `required` slot missing / listed in `missing`
   → raise `PromptTemplateError`, appended to `resolve_app_routes`'s `errors` and
   re-raised as `PlanValidationError`. **A residual prompt is never submitted to
   the app agent.** ("Missing" uses the same `_has_slot_value` as optional
   segments: only `None`/blank counts as absent, a numeric `0` counts as present.)
4. `_fill_template()`: targeted replacement of declared slot names only
   (`{name}`→value, not `str.format`, so cross-step `{var}` survive).
5. **`{var}` guard** (same as localize): any `{var}` surviving in the filled
   prompt must be ⊆ `produced` (upstream-bound vars), else raise.

### Why the syntax doesn't collide

Both the template slot `{place}` and cross-step `{var}` match
`flow_runner._VAR_RE`, but **fill happens at plan time, `render()` at runtime**:
fill replaces `{place}` with a literal value (`Navigate to Hong Kong
International Airport.`) or an upstream `{var}` token (`Navigate to {poi.name}.`).
No `{slot}` survives plan time; runtime `render()` only handles cross-step
`{var}`.

### Catalog pass-through

`build_catalog` (`agents/card_catalog.py`) trims capability fields by default.
`prompt_template` / `prompt_slots` are passed into the catalog digest **only when
present**, so `FlowPlanner._caps` can read them.

**Load-time validation.** While building the catalog, `_validate_prompt_template`
checks every templated capability and raises `ManifestValidationError` (fail
fast, loud) on: an undeclared `{placeholder}` (typos like `{palce}`), a declared
slot never used, a **required** slot that sits only inside a `[...]` segment
(droppable), an **optional** slot *not* wrapped in `[...]` (would leave a gap), or
unbalanced/nested brackets. This pulls authoring mistakes forward from "the
executed step trips `PromptTemplateError`" to load time.

## 4. Design decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| Missing required slot | **Hard fail** | Better not to run than to submit a residual/hallucinated prompt; keeps the submitted wording/routing deterministic (slot *values* are still LLM-extracted, so this guarantees intent routing, not value correctness) |
| v1 scope | **NL flow only** (`run_plan.py`/`FlowPlanner`) | The path where the prompt is freely synthesized and drifts most; the direct `python -m agents.native_runner <pkg> <goal>` entry uses the user's own words and is not templated yet |
| planner system prompt | **Unchanged** | The planner doesn't yet know the routing result, so it can't know which step gets a template; the extractor pulls values from the synthesized prompt — zero planner change, lowest risk |
| `example_prompts` | **Kept** | Few-shot fallback when no template is present |

## 5. TODO

- **Nested optional segments**: `_OPT_SEGMENT_RE` matches only non-nested
  `[...]`, so `[a[b]]` is unsupported. Flat segments cover current needs.
- Once the template mechanism is proposed into a formal card spec version,
  consider bumping each manifest's `spec_version`.

## 6. Verification

```bash
# Deterministic string (no LLM phrasing jitter)
uv run python scripts/run_plan.py --dry-run "用 Gemini 导航到香港国际机场"
#   1. [agent] Gemini/live_navigation -> Navigate to Hong Kong International Airport.

# Real-device smoke
uv run python scripts/run_plan.py --yes "用 Gemini 导航到香港国际机场"
```

A no-template capability (e.g. Qwen `foundation_llm`) should still go through
free synthesis + localize under `--dry-run` — behavior unchanged.
