# NL cross-app flow architecture

> 中文版：[`nl_flow.zh.md`](nl_flow.zh.md)

> One NL sentence → an auto-synthesized multi-app plan → execution. This doc is the **architecture deep-dive** (synthesis / three-stage routing / validation / execution / leg judge / handoff / route solidification).
> For the pipeline, CLI usage, caching, and a real-device worked example, see [`cross_app_planner.md`](cross_app_planner.md).
>
> Code: `agents/flow/flow_planner.py` / `agents/flow/flow_runner.py` / `agents/routing/capability_matrix_router.py` / `agents/flow/leg_judge.py` / `agents/routing/route_overlay.py` / `scripts/run_plan.py`.

---

## 1. Overview

```
NL request
  └─ FlowPlanner.plan()            synthesize plan (one LLM call, static one-shot, no step-by-step replanning)
       ├─ resolve_app_routes()     resolve each app step's app+capability via the three-stage router
       │    └─ _fill_prompt_template / _maybe_localize_prompt  fill the submit prompt
       ├─ validate + repair loop   local validation; on error, feed the errors back to an LLM repair round (≤3), re-route, re-validate
       └─ persist to manifests/_generated/*.yaml
  └─ FlowRunner.run()              execute steps in order
       ├─ app_step    → spawn `python -m agents.runtime.native_runner` subprocess (one leg = one app + one capability)
       ├─ ask_user    → collect human input in the terminal (renders a select_from pick list)
       └─ extract     → text LLM parses the previous leg's reply into structured JSON
       blackboard: {var}/{var.field} carries values between steps
```

Key design choices:

- **Static one-shot planning**: the planner emits the whole plan once. We do **not** do step-by-step replanning here.
- **Plan reuses the flow yaml shape** (`app_step` / `ask_user` / `extract` / `bind`), so **no new executor is needed** — `FlowRunner` runs it directly. There is no separate `inputs` block — the request is concrete, so literal values are baked straight into the step prompts.
- **Each app step is a fresh subprocess**; we don't reuse one long-lived RelayAgent across apps — plan cursor / chat history are single-card scoped.
- **The router is bypassed inside the subprocess**: `RELAY_FORCE_CAPABILITY` + `RELAY_INVOCATION_TEXT` make the sub-run skip the routing LLM call and go straight to plan building.

## 2. Plan synthesis (`FlowPlanner.plan`, `_PLANNER_SYSTEM`)

Input: the full-app catalog produced by `build_catalog()` (each capability's id / description / example_prompts / executable / `handoff_to_user_required` / `x_skip_wait_for_reply`) plus the user's NL.

Output plan (`json` fence, `temperature=0`):

```json
{
  "description": "<one-line plan summary>",
  "apps_required": [{"app_id": "...", "use_capability": "..."}],
  "steps": [ <step>, ... ]
}
```

**Two step kinds:**

- **App step**: `{id, app?, capability?, prompt, extract?, bind?}`
  - `app` / `capability` are **provisional**: the planner only decomposes the task and writes each step's concrete prompt; the final app/capability is decided later by the matrix router.
  - `extract` (optional, only when a downstream step consumes structured data): `{prompt, bind_to_array_key}`.
  - `bind` (optional, omit if nothing downstream needs it).
- **Ask-user step**: `{id, type:"ask_user", bind, prompt_header, select_from?, item_label?}` — hand control back to the human then continue; `select_from` renders a numbered pick list.

**Planner hard rules** (in the system prompt):

1. Use only ids that appear in the catalog; never invent.
2. Pass data across steps via an upstream `extract`+`bind`, referenced downstream as `{var}`/`{var.field}`; **every `{var}` must be produced by an earlier step's bind**.
3. When a reply is "a list the user should pick from" → insert `ask_user` + `select_from`.
4. **A `handoff_to_user_required` capability is never the final action of a non-terminal step**: if it is not the whole task's last step, it **must** be followed by an `ask_user` (show the agent's surfaced reply → collect the user's answer → then a follow-up app step that consumes the answer, **re-stating the full intent** because it runs a fresh agent session). If it **is** the last step (e.g. hailing the ride at the end), it may be terminal — its own in-app handoff is the user's final confirmation. An `x_skip_wait_for_reply` step captures no text reply, so give it no `bind`/`extract`.
5. Prefer the user's own wording; default each app-step prompt to that app's first `locale` language (only switch if the user explicitly asks for another); preserve proper nouns / addresses / product names / code / URLs / emails / ids / quoted literals in their original language.
6. A single-app request is fine — emit a one-step plan.
7. **`foundation_llm` is the general info/knowledge fallback**: information / Q&A / summarization / drafting / lookup tasks that no dedicated capability covers (explaining a GitHub repo, summarizing an arXiv paper, general knowledge) route to a `foundation_llm` capability instead of being declared unsatisfiable.
8. Return `{"unsatisfiable": true, "reason": "..."}` only when the task **requires a concrete device/app action** no capability provides (posting to a chat platform the catalog lacks, taking a camera photo) **and `foundation_llm` cannot stand in**.

## 3. Three-stage routing (`capability_matrix_router.route`)

The reusable version of the single-app NL routing strategy. `docs/app_capability_matrix.csv` is the **source of truth for cap × app membership**; the catalog is only an availability check (drops matrix entries pointing at a now-missing (app,cap) pair).

- **Stage 0 — solidified short-circuit** (route overlay, see §9): when `route_key`+`overlay` are passed, the solidification table is consulted **before** the three stages — a confident hit whose `(app,cap)` is still in the catalog is returned directly, **zero LLM calls**. A cold / low-confidence key falls through to the three stages unchanged.
- **Stage 1 — vertical prefilter** (`_stage1_prefilter`): give the LLM a vertical-capability menu (id+desc) that **excludes `foundation_llm`**; pick ≤3 most-likely cap ids. If no vertical fits, return an empty list.
- **Stage 2 — rerank** (`_stage2_rerank`): expand the stage-1 caps × matrix-authorized runnable apps into (app, capability) options (with desc / examples / locale); the LLM picks the **single** best and writes the goal sentence.
  - **Early exit**: a single remaining option is returned without an LLM call.
  - LLM returns `kind:"none"` or picks an off-shortlist pair → fall back to stage 3.
  - Matched a vertical cap but the matrix authorizes 0 runnable pairs → raise (don't swallow silently).
- **Stage 3 — foundation fallback** (`_stage3_foundation`): entered only when no vertical fits. **This is a structurally separate stage, not just a hint in a prompt** — pick the best general assistant among foundation_llm apps and write the goal.
  - **Escape hatch (not an unconditional catch-all)**: a foundation assistant only produces a **text answer**; if the task requires a concrete device/OS **side-effect** a chat assistant can't do (file management, renaming/moving/deleting files, changing system settings, driving another app's UI), stage-3 returns `kind:"none"` → raises `FoundationNotApplicable`. `_route_one_step` treats it as a **coverage gap** (tags `x_coverage_gap`, adds to gaps) so repair can retry; if still unclosed, `_apply_mw_fallback_to_gaps` converts it to a **MobileWorld leg** instead of force-fitting it into foundation_llm. Example: "rename `bid_`-prefixed files in Download by creation date".

`route(..., preserve_goal=True)`: flow planning uses the router only for app/capability selection and **keeps the planner's own templated prompt** (forces `goal` back to the original NL so the router doesn't rephrase it).

> **Locale policy**: the goal sentence defaults to the chosen app's first locale language; only switch if the user explicitly asks; preserve proper nouns/addresses/etc. All three stages carry this.

## 4. Filling the prompt after routing (`_route_one_step`)

The entry first computes and stamps `step["x_route_key"]` (the normalized-prompt sha1, fed to the §9 solidification loop; it reuses an already-persisted key to avoid drift on a cache re-run) and passes `route_key`+`overlay` to the router. Only after routing is the capability — and thus which template applies — known, so the prompt is filled here:

- capability has `prompt_template` → `_fill_prompt_template()` (extract slots, fill the fixed template), **skipping** `_maybe_localize_prompt` (the template is already authored in the app's locale). Any failure is a hard error.
- otherwise → `_maybe_localize_prompt()`: if the free-synthesized prompt is locale-incompatible and the user didn't explicitly pin a language, one LLM call rewrites it to the target language. The rewrite **must keep the `{var}` placeholder set unchanged**, else it is discarded and the original kept.

Full prompt-template mechanism: [`prompt_template.md`](prompt_template.md).

## 5. Local validation + LLM repair (`_validate` → `_repair`)

After synthesis (incl. routing) we run local validation. What it checks:

- each step is an object; `id` unique; `bind` name unique.
- app step: `app`/`capability`/`prompt` non-empty; `app` in the catalog; `capability` belongs to that app.
- every `{var}` in `prompt` / `extract.prompt` must be **produced by an earlier step**.
- **Rule 4**: a mid-flow `handoff_to_user_required` leg must be followed by `ask_user`; a handoff leg that is the **last step** is a valid terminal.
- an `x_skip_wait_for_reply` cap cannot carry `bind`/`extract`.
- ask_user: must have `bind`; `select_from` must be a **string** bound by an earlier step (resolved to its **root** name, so `{var}` / `var.field` forms are accepted; a non-string `select_from` is a recorded error, not a crash); `prompt_header` `{var}`s must be already bound.

**Repair loop** (`plan()` → `_repair`): on a routing **or** validation error, the broken plan + its error list are fed back to the LLM for a corrected plan (same schema), which is then re-routed and re-validated — up to `_REPAIR_ROUNDS` (3) rounds. Only after the rounds are exhausted does `plan()` raise `PlanValidationError` (with the full error list) — and only when MW fallback is off; with it on, an unrepairable plan degrades to a whole-request MW leg instead of raising (see §10). A repair round may legitimately return `{"unsatisfiable": ...}`. (`validate_plan()` itself, used directly on a **cached** plan, still hard-fails without repair — a cached plan was already valid when persisted.) Malformed model JSON (raw control chars in strings) is tolerated via `json.loads(strict=False)` in `_parse_fenced_json`.

The routing phase also cleans up: `_drop_unused_no_reply_binds` (strip a decorative `bind`/`extract` on a no-reply step that nothing downstream references) and `_refresh_apps_required` (rebuild `apps_required` from the actual routing result).

## 6. Execution (`FlowRunner.run`)

Steps run in order; the blackboard `self.bb` starts empty and grows with each step's bind. `render()` does `{var}`/`{var.field}` substitution (missing key → `''`).

Before the first step, `nl_flow.execute_plan` force-stops every app the plan touches (best-effort; a kill failure never blocks the run) so no leg resumes a stale background session. Disable with `RELAY_PREKILL_APPS=0`.

### 6.1 Runtime failure recovery (`leg_recovery`, default ON)

A failed app leg no longer kills the flow outright: `FlowRunner` classifies the failure and climbs a bounded ladder — **retry** (same app/capability, fresh conversation; a `route_fail` retry first rewords the prompt with one cheap LLM call, except for `prompt_template` capabilities whose wording is fixed) → **reroute** (three-stage router re-run with the failed (app, capability) pairs excluded; a target using a `prompt_template` is skipped in v1) → **MobileWorld fallback** (the failed leg becomes a runtime `type: mobileworld` leg, same machinery as plan-time fallback) → **partial-success terminal** (no more bare tracebacks: `flow_report.json` at the flow root records per-step outcomes, recovery attempts and the blackboard keys accumulated so far; a fatal failure still raises after writing it, a judge-only failure ships the best attempt and continues, matching the old advisory semantics).

Failure taxonomy (`R0`): `env_fail` (subprocess died before the run loop — device/IME layer; never recovered), `route_fail` (leg judge says wrong feature / off-goal, via the judge's new `failure_kind: wrong_feature`), `app_fail` (right feature, no delivery: missing needed reply, bad terminal state, or judge `failure_kind: app_error`).

**Safety red line**: a capability with `handoff_to_user_required: true` gets the retry tier ONLY — never rerouted (a different app would redo user-visible preparation), never handed to MobileWorld (`general_e2e` has no handoff contract and could cross an irreversible action on its own).

**Knobs**: `RELAY_RECOVERY` (default `1`; `0` restores fail-fast — `run_benchmark_test.py` forces `0` unless `--recovery`) / `RELAY_RECOVERY_MAX_RETRIES` (per leg, default 1) / `RELAY_RECOVERY_MAX_LEGS` (extra legs per flow, default 2) / `RELAY_RECOVERY_TOKEN_BUDGET` (default 15000, read off each attempt's summary `token_usage`). Artifacts: `recovery.json` next to the original attempt's trajectory (per-tier attempt log; each entry carries tier/target/outcome/detail/**token cost**), retry/reroute attempts land in sibling leg dirs suffixed `_retryN` / `_reroute`. Benchmark side (R4): `run_benchmark_test.py --recovery` harvests a per-row `recovery` block off the flow root's `flow_report.json`, and `summary.json`/`summary.md` report first-try vs final success, per-tier hit rate and recovery-token inflation.

**App step (`_run_app_step`):**

- Each leg is a fresh `python -m agents.runtime.native_runner <app> <prompt>` subprocess.
- Subprocess env: `RELAY_FORCE_CAPABILITY`/`RELAY_INVOCATION_TEXT` (bypass router), `RELAY_SKIP_OPEN_APP=1`+`RELAY_AGENT_LAUNCH=1` (deferred-launch: cold-launch happens in the agent's first predict, so process/leg startup is excluded from the leg's wall-clock), `RELAY_TRAJ_DIR` (pin traj.json / steps/ / agent_reply.json straight into this leg's `NN_<id>/` dir — no global `user_task` scratch, no post-run copytree), `RELAY_REPLY_OUT` (reply JSON), `RELAY_SUMMARY_OUT` (summary), `RELAY_WALL_OUT` (the agent writes the framework-excluded `wall_clock.json`).
- stdin is `DEVNULL`: a trailing ask_user handoff closes cleanly on EOF instead of blocking the flow.
- **Per-leg traj preserved**: each flow run has its own traj root `traj_logs/<ts>_plan_<app1>_<app2>.../`, one `NN_<id>/` per leg. The subprocess writes trajectory artifacts directly into that leg dir via `RELAY_TRAJ_DIR`; the native runner skips its global backup rotation when pinned. See [`trajectory_logging.md`](trajectory_logging.md).
- **Reply / hard signals**: read the reply from `RELAY_REPLY_OUT`. A leg that needs a reply (`bind`/`extract`) but captured none → raise. A no-reply leg goes through `_assert_output_free_step_completed`: requires `rc==0` and last_action ∈ {ask_user, answer} or (finished and goal complete), else raise.
- **Leg judge** (semantic layer, see §7): the "confidently wrong" check on top of the hard signals.

**Ask-user step (`_run_ask_user`):** `select_from` renders a numbered list (`item_label` template controls each item's display); user input goes through `_resolve_choice` (number / substring match against label or name / empty picks the first); or plain freeform. EOF → empty.

**Extract (`_extract`):** runs a text-only chat completion against the same `.env` endpoint, parsing the previous leg's reply into fenced JSON; `bind_to_array_key` pulls one key out of the result object.

## 7. Leg judge (`leg_judge.py`, semantic outcome check)

A **leg** is one native-runner sub-run pinned to one app + one capability. The hard signals in `flow_runner` (crash / empty reply / non-terminal state) only catch **overt** failures — they can't tell a confidently-wrong answer from a correct one.

- Mirrors MobileWorld's `BaseTask.is_successful` contract (`-> (score, reason)`, 1.0 success / 0.0 failure), but open-world apps have no per-task ground-truth oracle, so **a VLM reads** the leg's goal + the captured reply + the final screen and classifies into three: **`loading`** (still in progress, outcome undetermined, **not a failure**) / **`success`** / **`failure`**.
- **Takes the last n frames** (`final_frames`, default 2, the StepLogger's pre-action PNGs): sending the last two (not one) lets the judge tell a stuck/loading screen apart from a settled final state.
- **Handoff leg** (terminal_action == `ask_user`) switches to the `_SUCCESS_HANDOFF` definition — deferring the final decision to the user is **expected, not a failure**; non-handoff uses `_SUCCESS_OUTCOME` (the action was actually carried out / the info is actually shown).
- **Loading-retry** (`flow_runner._judge_leg`): on a first `loading` verdict (e.g. a map spinning up after live_navigation's CTA), wait `RELAY_LEG_JUDGE_LOADING_WAIT` (2s), `screencap()` a **fresh** frame, and re-judge up to `RELAY_LEG_JUDGE_LOADING_RETRIES` (3) times, stopping as soon as it settles. Only `loading` pays this cost.
- **Best-effort**: any error (no frames / LLM down / unparseable) returns `UNKNOWN` (`judged=False`). **The caller must never let a judge failure abort the flow** — surface it (per CLAUDE.md fallback policy) and move on. `LegVerdict.score` (1.0/0.0/-1.0) is persisted to `leg_verdict.json` next to the leg trajectory. `RELAY_LEG_JUDGE=0` disables it.
- **Folds back into the table**: after the verdict is written, `_judge_leg` calls `overlay.record(step["x_route_key"], ..., verdict.status)`, folding it into the route-solidification loop (§9). This is the **only** writer of the table, reuses the existing verdict, and adds no LLM calls.

## 8. Phase-A / Phase-B handoff

- **Phase A (current)**: handoff at flow granularity — a handoff leg is followed by a flow-level `ask_user`, then a **fresh** leg consumes the answer. In-app session state is lost, so the follow-up leg must re-state the full intent.
- **Phase B (TODO, commented in `flow_runner`)**: same-session handoff round-trip. When a leg carries `resume:true`, don't close stdin with EOF — keep the subprocess alive and wire a flow⇄agent channel (fifo/file) so the in-app agent's handoff ask_user blocks on the answer and resumes `predict()` in the **same conversation**, preserving in-app state.

## 9. Route solidification (route overlay, trace-guided)

Turns the §7 leg verdict from "log only" into an input to the router: an `(intent → app/capability)` decision the judge repeatedly confirms `success` is **solidified into a table lookup** so the next time the same intent shows up the router returns it with zero LLM calls; one that keeps `failure`-ing is auto-invalidated and falls back to the three stages. Code in `agents/routing/route_overlay.py`, store at `traj_logs/route_overlay.json` (a git-ignored **learned, non-authoritative** artifact; the matrix CSV stays the source of truth, and promoting high-confidence entries back into it is a separate human-reviewed step).

**The loop:**

```
synthesized prompt ──compute_route_key(mode a/b, default b)──► step["x_route_key"]  (persisted in the yaml)
   │  §3 Stage-0 short-circuit reads it                       │  §7 leg judge writes it
   ▼                                                          ▼
route(route_key, overlay):                            end of _judge_leg:
  lookup(key) hits & (app,cap)∈catalog                  overlay.record(x_route_key, app, cap, status)
    → return directly, 0 LLM                              success→reset consec_fail / failure→consec_fail++
  else → three-stage LLM                                  atomic write of route_overlay.json
```

**Solidification test (`lookup`)**: an `(app,cap)` with `success ≥ MIN_HITS(3)` and `success_rate ≥ RATE(0.8)` and `consec_fail < MAX_FAILS(2)` is returned (highest-success wins on ties); otherwise None → fall back to the LLM. A cold / low-confidence table never short-circuits, so the **P0 "shadow period" is built in** — `record` keeps accumulating data while `lookup` stays inert until an entry qualifies (overlay on vs. off behaves identically until then).

**Self-correction**: `MAX_FAILS` consecutive `failure`s pause that entry's solidification (re-route via LLM); one `success` resets `consec_fail` and revives it. `loading`/`unknown` are recorded but **neutral** (not in the rate denominator, don't touch consec_fail).

**Stale guard**: a solidified hit whose `(app,cap)` is no longer in the catalog (manifest change / retired capability) does **not** short-circuit — it warns and routes live.

**route_key (`compute_route_key`, `RELAY_ROUTE_KEY_MODE`, default `b`)**:

- **mode `a` (value-bearing)**: the `sha1[:16]` of the normalized (lowercased + whitespace-collapsed) synthesized prompt. Reuses only repeated / near-identical intents, isomorphic to the exact-string plan cache.
- **mode `b` (value-independent, default, P3)**: `sha1` of `provisional_cap | provisional_app | locale_bucket`, with a `b:` prefix. Keyed on the planner's **provisional capability** + app hint + **request-locale bucket** (coarse CJK / latin), with no literal values — so "navigate to People's Square" and "navigate to Hongqiao Airport" **share one solidified route** (cross-intent reuse, saving the three stages even on a plan-cache miss), while Chinese navigation (→ Amap) and English navigation (→ Gemini) **never collapse** thanks to the locale bucket. Falls back to `a` whenever there is no provisional capability, so it degrades safely.

**Key stability**: `_route_one_step` uses `step.get("x_route_key") or _compute_route_key(prompt, provisional_cap=..., provisional_app=...)` — on a cache re-run `prompt` is already the filled text and `capability` is the final routed one (not the provisional), so reusing the persisted key avoids drift. Both modes' keys coexist in one store (the `b:` prefix distinguishes them).

**Best-effort**: any lookup/record error only warns, a corrupt JSON is treated as empty, and neither ever breaks planning or a flow run. The atomic write (temp + `os.replace`) guarantees a crash never leaves half-JSON.

**Three cache layers (shallow → deep, each saves more LLM)**:

| Layer | Hit saves | Trigger | Log evidence |
| --- | --- | --- | --- |
| plan cache | the plan-synthesis LLM | normalized-NL exact-string hit in `manifests/_generated/*.yaml` | `cache hit → ...yaml` |
| route solidification | the 3 three-stage LLM calls | `success ≥ 3` and `rate ≥ 0.8` and `consec_fail < 2` | `route solidified -> ... (0 LLM)` |
| (baseline) device execution | — | always happens → feeds leg judge → overlay | leg judge + overlay recorded |

**Promote (`scripts/routes/promote_routes.py`, read-only)**: surfaces the high-confidence routes the trace has learned so a human can decide whether to fold them into the matrix — it **never writes the matrix** (the CSV is the hand-maintained source of truth). It scans the overlay at a *higher* bar (`RELAY_PROMOTE_MIN_HITS` default 5 / `RATE` default 0.9), lists each `(intent → app/cap)` and whether it is **already authorized** in the matrix (the learned preference agrees) or **not listed** (a candidate ✓ or a stale entry to ignore). `--csv` also emits review rows to hand-paste. Pure logic — no device/LLM/network.

**Switches / thresholds**: `RELAY_ROUTE_OVERLAY` (default 1) / `RELAY_ROUTE_OVERLAY_PATH` / `RELAY_ROUTE_KEY_MODE` (default `b`) / `RELAY_ROUTE_SOLIDIFY_HITS` (3) / `RELAY_ROUTE_SOLIDIFY_RATE` (0.8) / `RELAY_ROUTE_MAX_FAILS` (2) / `RELAY_PROMOTE_MIN_HITS` (5) / `RELAY_PROMOTE_MIN_RATE` (0.9).

## 10. MobileWorld fallback (when no capability covers a leg)

RA's routing rests on a **hand-maintained manifest + capability matrix**. When a leg (or the whole request) is **covered by no app/capability**, rather than give up, hand it to **MobileWorld's `general_e2e`** — a **manifest-free general end-to-end UI agent** that opens apps and navigates from the current screen to accomplish any goal (the fork is already pinned as the `mobile-world` dependency in `pyproject.toml`, installed under `.venv/.../mobile_world/`).

**Trigger (planner, every unsatisfiable outcome):**

- **Coverage gap** (capability matched but no app authorized in the matrix, `NoRunnableAppForCapability`; or **stage-3 judged a device action not a foundation task**, `FoundationNotApplicable`): `_route_one_step` tags the step `x_coverage_gap` but **still runs the repair rounds** (repair may re-route the gap to a real capability such as `foundation_llm` — preferred over MW). If a gap survives all repair rounds, `_apply_mw_fallback_to_gaps` converts exactly the tagged steps into MW legs (`_to_mw_leg`) in place, re-validates, and returns the now-**satisfiable** plan instead of `{"unsatisfiable"}`. If the gap-converted plan is still invalid (e.g. a downstream `{var}` only the dropped capability could bind), it too falls back to a whole-request MW leg rather than `{"unsatisfiable"}`.
- **LLM judges the whole request unsatisfiable** (`plan()`/`_repair` returns `{"unsatisfiable"}`, so there are no steps): degenerates to "whole request = one MW leg" via `_mw_whole_request_plan` (a single leg whose `prompt` is the original request).
- **Repair exhausted, still invalid** (a non-coverage-gap validation error — unfillable prompt template, a handoff structure RA can't satisfy): after `_REPAIR_ROUNDS` rounds, with MW fallback on the planner **no longer raises `PlanValidationError`** — it falls back to a whole-request MW leg (`_mw_whole_request_plan`); only with fallback off does it raise.

**MW leg shape** (new step type `type: mobileworld`): keeps `id`/`prompt`/`bind`/`extract`; `app` is only a **prelaunch hint** (no capability to route); carries `x_fallback_reason`. `_validate` uses `_validate_mw_leg`: requires a non-empty `prompt` and upstream-bound `{var}`s, skipping app/capability/handoff checks. `resolve_app_routes` skips MW legs (including on a cached re-run).

**Execution (`FlowRunner._run_mobileworld_step`)**: `FlowRunner` starts **one** MobileWorld server for the whole flow (`_ensure_mw_server` / `_teardown_mw_server` in `run()`'s `finally`) and reuses it across MW legs. Each leg shells out to `scripts/run_mobileworld.py` with `--no-start-server --server-url <flow-managed>` plus `--agent-type general_e2e --output <leg_dir>` (trajectory lands at `<leg_dir>/user_task/traj.json`), `--app` if there's a hint else `--no-prelaunch`. Afterward `_harvest_mw_traj` takes the **last `answer` action's text** as the leg reply → feeds the blackboard (same `bind`/`extract` path as an app leg), and writes `summary.json` + `agent_reply.json`. The **leg judge** runs as usual (`final_frames` falls back to `user_task/screenshots/*.png` when `steps/` is absent); MW legs are **not solidified** (not a matrix route, no `x_route_key`). Flow-level LLM calls fold into the leg's `traj.json` as usual.

**Switches / knobs**: `RELAY_MW_FALLBACK` (default `1`; `0` or `run_plan.py --no-mw-fallback` disables it, restoring the old unsatisfiable-exit behavior) / `RELAY_MW_SERVER_URL` (default `http://127.0.0.1:6800`) / `RELAY_MW_MAX_ROUND` (default 25) / `RELAY_MW_TIMEOUT` (default 600). The preview marks MW legs with `[MobileWorld fallback]`.

**Deferred**: route solidification for MW legs.
