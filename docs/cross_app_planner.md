# Auto cross-app planner (`run_plan.py` / `FlowPlanner`)

> 中文版：[`cross_app_planner.zh.md`](cross_app_planner.zh.md)

> One NL sentence → LLM **auto-synthesizes** a cross-app plan → validate → persist → preview & confirm → run on a real device.
>
> How it relates to the existing entries: `run_nl.py` **picks** one of the **hand-written** flows / single-app capabilities; `run_flow.py` runs a **hand-written** flow YAML directly; `run_plan.py` is for when there is **no** matching hand-written flow — it has the model **synthesize** a multi-app plan on the spot. All three share the same `FlowRunner` executor and flow schema.

---

## 1. What it solves

Before this, RelayAgent's cross-app capability relied on **hand-authored** YAML under `manifests/_flows/`: which apps, in what order, which capability, and how data binds between legs were all written by hand. The `run_nl.py` router could only **pick one** of those existing flows (its prompt explicitly says `do NOT invent ids`).

So given a cross-app instruction with **no matching flow**, the system couldn't autonomously decompose the goal into a multi-app step sequence — the router finds no match and falls back to a single app, or picks the wrong one.

`run_plan.py` fills exactly that gap: **given the full app/capability catalog, have the LLM dynamically produce the steps + bind relations** (i.e. have the model produce what used to be the hand-written flow YAML), then hand it to the existing `FlowRunner`. **No new executor — only a new "planning" layer.**

---

## 2. Pipeline

```
one sentence
  │
  ├─(1) build_catalog()            full app + capability list (reused from run_nl.py)
  │
  ├─(2) cache lookup               exact string match in manifests/_generated/; skipped by --no-cache
  │        hit ┐
  │           └────────────────────────────────┐
  ├─(3) FlowPlanner.plan()         miss: LLM synthesize → fenced JSON → local validation
  │        │                                     │
  │        ├─ invalid → hard-fail + exit (repair is a TODO)
  │        └─ unsatisfiable → print reason + exit
  │                                              │
  ├─(4) persist                    write plan YAML to manifests/_generated/
  │                                              │
  ├─(5) preview + confirm ←─────────────────────┘
  │        default N; non-interactive stdin (EOF) = don't execute; --yes skips; --dry-run stops here
  │
  └─(6) FlowRunner.run()           one mw test per app leg, reusing the persistent MobileWorld server
```

Files involved:

| File | Role |
| --- | --- |
| [`scripts/run_plan.py`](../scripts/run_plan.py) | CLI entry: cache / persist / preview / confirm / recording / dispatch |
| [`agents/flow_planner.py`](../agents/flow_planner.py) | `FlowPlanner`: catalog → prompt → LLM → JSON → validation (with a repair TODO stub) |
| [`agents/flow_runner.py`](../agents/flow_runner.py) | the existing executor, reused as-is (generated plans share the hand-written flow schema) |
| [`manifests/_generated/`](../manifests/_generated/) | generated-artifact + cache dir; `.gitignore` keeps its contents out of version control |

---

## 3. Generated plan schema

The output **reuses the flow YAML shape**, so it can be fed straight to `FlowRunner`. The only difference from a hand-written flow: **no `inputs` block** — the sentence is concrete, so literal values are baked directly into each step's `prompt`; cross-leg data flow still uses `extract` / `bind` / `{var}`.

Field order after persisting (fixed by `run_plan.py:_persist`):

```yaml
flow_id: gen_<8-char hash>          # auto-derived
source_request: <normalized original sentence>   # used for exact-match caching
description: <one-line summary>
apps_required:                      # for validation / preview display only
  - {app_id: ..., use_capability: ...}
steps: [ ... ]
```

Each entry in `steps` is one of the following two kinds:

**App step** (drives one app's agent for one capability)

```yaml
- id: find_bookstores
  app: com.xingin.xhs                # must be an app_id in the catalog
  capability: qa_community_knowledge  # must be a capability id that exists on that app
  prompt: "在上海找三家评价好的小众书店，列出店名、地址和简短推荐理由"
  extract:                           # optional: only when a later step consumes this reply's structured data
    prompt: "parse into a JSON array [{name, address}]"
    bind_to_array_key: bookstores    # pull this key out of the extracted JSON (works for arrays/strings alike)
  bind: bookstore_list               # optional: store this step's result as a variable, referenced downstream as {bookstore_list}
```

**Ask-user step** (hand control to the user, then continue)

```yaml
- id: pick_bookstore
  type: ask_user
  bind: selected_bookstore
  prompt_header: "请从以下推荐的小众书店中选择一家："
  select_from: bookstore_list        # optional: render a numbered pick list from that list
  item_label: "{name}（{address}）"    # optional: how to render each list item
```

Templating: `{var}` and `{var.field}` resolve against the blackboard (`FlowRunner`'s `render()`). The blackboard starts empty (no inputs) and grows with each step's `bind`.

---

## 4. Planner rules (baked into the system prompt)

Hard constraints `FlowPlanner._PLANNER_SYSTEM` gives the model:

1. **Use only `app_id` / capability ids that appear in the catalog — never invent them.**
2. **To pass data across steps**, give the upstream app step an `extract` + `bind`, then reference it downstream via `{var}` / `{var.field}`; **every `{var}` referenced must be produced by an earlier step.**
3. When a step's reply is a **list** the user should choose from, insert an `ask_user` with `select_from`.
4. **For a `handoff_to_user_required` capability:**
   - If it is **not** the final step of the whole task → it **must** be immediately followed by an `ask_user` (show the agent's surfaced message via `prompt_header`, collect the user's answer), then another app step that consumes the answer (**re-state the full intent** in that step's prompt, since it runs as a fresh agent session).
   - If it **is** the final step (e.g. hailing the ride at the very end) → it **may** be terminal: its own in-app handoff is the user's final confirmation, so no trailing `ask_user` is needed.
5. Prefer the user's own wording in prompts; only fill obvious gaps; bake concrete values from the request directly into the prompt.
6. A single-app request is fine too — emit a 1-step plan (`run_plan` is a superset of `run_nl`).
7. When no combination of available apps can satisfy the request, return `{"unsatisfiable": true, "reason": "..."}`; the preview stage reports it honestly and does not execute.

---

## 5. Local validation

`FlowPlanner._validate()` blocks bad plans before execution (returns an error list; empty = valid):

- `steps` is a non-empty list;
- each app step: `app` / `capability` / `prompt` present, `app` exists in the catalog, `capability` exists on that app;
- every `{root_var}` referenced in `prompt` / `extract.prompt` / `prompt_header` was already bound by an earlier step (**blocks dangling references**);
- an `ask_user`'s `select_from` points to an already-bound variable;
- `bind` names are unique and `id`s don't repeat;
- **the rule-4 check**: if a `handoff_to_user_required` capability is **not** the last step, the next step must be an `ask_user` (a terminal one at the end is allowed).

> **Validation failure = hard-fail + exit**, printing the error list + the raw plan. **The LLM repair loop is a TODO** (`FlowPlanner._repair` is a stub), deliberately unimplemented so a bad plan never executes silently.

---

## 6. Handoff round-trip: phase A first, then B

"after `handoff_to_user`, be able to switch back to the agent" has two granularities; this version ships A and leaves a seam for B:

- **Phase A (shipped)**: the handoff leg ends → a flow-level `ask_user` collects the user's answer → the next agent leg is a **fresh `mw test`** (same app or a different one) with the answer + full intent re-stated in the prompt. Reuses the existing `FlowRunner` structure. **Limitation**: a same-app mid-task handoff cold-launches and clears history, losing in-app half-finished state.
- **Phase B (seam only, not wired)**: keep the handoff leg from terminating — replace `stdin=DEVNULL` in `flow_runner._run_app_step` with a flow⇄agent channel; the agent's handoff `ask_user` (inside `relay_agent.py`) no longer ends on EOF but blocks reading the answer the flow pipes back, then resumes `predict()` in the **same conversation**. Both spots carry `# TODO(phase-B):` markers.

> In-app handoff today: when the agent reaches the `handoff` step it first calls `_maybe_persist_reply()` (writes the reply to `RELAY_REPLY_OUT`), then emits `action_type="ask_user"`; in a flow leg stdin is DEVNULL → immediate EOF → the subprocess ends with the reply already persisted.

---

## 7. Cache

- **Persist**: a validated plan is written to `manifests/_generated/<slug>_<hash8>.yaml`, carrying `source_request` (the normalized original sentence).
- **Reuse**: before planning, scan `_generated/`; if a plan's **normalized `source_request` exactly matches**, reuse it directly (still goes through preview + confirm, no LLM call). Skipped by `--no-cache`.
- **Semantic reuse is a TODO**: falling back to embedding / LLM similarity when the exact string misses — `run_plan.py:_cache_lookup` leaves a `# TODO(semantic-cache):` hook, not yet implemented.

---

## 8. Usage

```bash
# basic: synthesize → preview → ask y/N → execute
uv run python scripts/run_plan.py "在上海找三家评价好的小众书店，挑一家打车过去"

# plan + preview only, don't execute (no device, one LLM call)
uv run python scripts/run_plan.py "..." --dry-run

# skip the confirm and execute (automation / real-device batch runs)
uv run python scripts/run_plan.py "..." --yes

# ignore the cache, force regeneration
uv run python scripts/run_plan.py "..." --no-cache

# record the screen (parent-owned, continuous across legs; auto-enables RELAY_SKIP_STEP_SCREENSHOT)
uv run python scripts/run_plan.py "..." --record
uv run python scripts/run_plan.py "..." --record /path/to/dir

# args after `--` are forwarded to each underlying mw test
uv run python scripts/run_plan.py "..." -- --step_wait_time 0.3
```

**Flag reference**

| flag | effect |
| --- | --- |
| `--dry-run` | stop after synthesize + preview; don't execute |
| `--yes` / `-y` | skip the y/N confirm |
| `--no-cache` | don't reuse a cached plan in `_generated/`; force regeneration |
| `--record [DIR]` | adb screen recording; defaults to `traj_logs/recordings/<ts>/` |
| `-- <args>` | everything after `--` is forwarded to the underlying `mw test` |

**Environment**

- Planning uses `.env`'s `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` (= `qwen`), the same endpoint as the `run_nl` router.
- Execution reuses the persistent MobileWorld server (`scripts/_mw_server.py:ensure_server` injects `--aw_host`), consistent with `run_nl` / `run_flow`.
- Real-device requirements as elsewhere in the project: adb + USB debugging + `com.android.adbkeyboard/.AdbIME`; `RELAY_ANDROID_SERIAL` selects the device.

**Non-interactive behavior of mid-flow `ask_user`**: a flow-level `ask_user` reads the parent process's stdin. With `< /dev/null` (or a piped EOF), a pick step **auto-takes the first candidate** and a freeform step takes the empty string — suitable for `--yes` batch runs. To pick by hand, run interactively in a real terminal.

**Safety**: default N; a non-interactive stdin is treated as don't-execute. A `handoff_to_user_required` capability (e.g. hailing a ride, placing an order) stops **before the irreversible CTA** and hands back — it does not actually order/pay.

---

## 9. Known limitations / TODO

| Item | Status | Location |
| --- | --- | --- |
| LLM repair loop | TODO (hard-fail instead) | `flow_planner.py:_repair` + `# TODO(repair):` |
| Semantic cache reuse | TODO (exact string match only) | `run_plan.py:_cache_lookup` `# TODO(semantic-cache):` |
| Phase B same-session resume | seam only | `# TODO(phase-B):` in `flow_runner.py` / `relay_agent.py` |
| Static one-shot planning | by design | no step-by-step / failure replanning; doesn't adapt to surprising leg output |
| Plan complexity | no hard cap | only a `logger.warning` at ≥4 app legs |

---

## 10. A real-device run (worked example)

Input: `"在上海找三家评价好的小众书店，挑一家打车过去"` (Pixel 9, `--yes`, stdin `</dev/null`).

The synthesized plan (same structure as the hand-written [`xhs_to_amap_place.yaml`](../manifests/_flows/xhs_to_amap_place.yaml)):

```
1. [agent]    Xiaohongshu / qa_community_knowledge  →  extract → bind bookstore_list
2. [ask_user] pick 1 from bookstore_list            →  bind selected_bookstore
3. [agent]    Amap / hail_ride (terminal handoff)
```

Execution trace:

- **Leg 1 (点点 qa)**: 点点 replies ~915 chars → extract pulls 3 stores `[{犀牛书店,…},{i人书房,…},{1691 Coffee Bar,…}]` → bind `bookstore_list`. task wall_s **79.1s**.
- **ask_user**: stdin EOF → auto-takes the first, **犀牛书店** → bind `selected_bookstore`.
- **Leg 2 (Amap hail_ride)**: prompt renders to `帮我叫一辆车去 犀牛书店，地址是 苏州河畔老建筑` (`{selected_bookstore.name}` / `.address` cross-leg passing works) → the agent reaches handoff and **stops before the ride-confirm CTA, does not order**. task wall_s **68.6s**.

The whole cross-app task ≈ **2.5 minutes**, exit 0 throughout with no errors.

---

## 11. Change notes (introduced here)

This adds the "auto-synthesize a cross-app plan" layer; the full set of changes:

**Added**

| File | Content |
| --- | --- |
| `agents/flow_planner.py` | `FlowPlanner`: catalog → system prompt → LLM → fenced JSON → local validation (`_validate`). `PlanValidationError` carries the error list. `_repair` is a TODO stub. |
| `scripts/run_plan.py` | CLI entry: exact-string cache / synthesize / persist / preview / confirm / recording / dispatch to `FlowRunner`. Flags: `--dry-run` `--yes` `--no-cache` `--record` `-- <forward>`. |
| `manifests/_generated/.gitignore` | keeps generated plans / cache out of version control, retaining only `.gitignore` itself. |
| `docs/cross_app_planner.md` / `docs/cross_app_planner.zh.md` | this document (English / Chinese). |

**Modified**

| File | Change |
| --- | --- |
| `agents/flow_runner.py` | ① `# TODO(phase-B):` seam comment (at `stdin=DEVNULL`). ② new `_traj_stem()`: hand-written flows still use the file stem; auto-synthesized plans (marked by `source_request`, no `inputs`) name the traj dir `plan_<app1>_<app2>…` instead, avoiding a long NL-slug filename as a directory name. |
| `agents/relay_agent.py` | `# TODO(phase-B):` seam comment on the handoff branch (no logic change). |
| `CLAUDE.md` | added the run_plan entry under `跑测试`; added an "auto cross-app planning" overview section pointing here; "three → four entry scripts". |
| `README.md` / `README_zh.md` | added run_plan to the scripts listing; added an "auto-synthesize a cross-app plan" subsection after the NL entry point. |

**Design decisions (why)**

- **Static one-shot planning** rather than step-by-step / ReAct: reuses the existing `FlowRunner`, minimal change, immediately shippable; the cost is no adaptation to surprising leg output.
- **A dedicated `run_plan.py`** rather than folding into `run_nl`: zero intrusion on the existing NL routing.
- **Preview + confirm (default N)**: cross-app carries irreversible side effects (ride-hailing / ordering), so a human must see and approve before execution.
- **Hard-fail on validation, repair left as TODO**: better to abort than let a bad plan execute silently.
- **Handoff may be terminal at the end, ask_user forced only mid-flow**: matches the hand-written `xhs_to_amap_place` ending on hail_ride; the final leg's in-app handoff is itself the terminal confirmation.
- **Cache exact-string first, semantic reuse left as TODO**: get the main path working before investing in caching.
