<h1 align="center">Productization Roadmap</h1>

<p align="center">
  <b>From "measurable in a paper" to "usable every day" — five phases ordered by return on effort</b>
</p>

<p align="center">
  <b>English</b> | <a href="roadmap.zh.md">中文</a>
</p>

> An engineering roadmap for contributors: five phases ordered by return on effort, each anchored to existing seams in the codebase and gated by the repo's own A/B evaluation infrastructure. Discussion and claiming work happens in GitHub Issues.

## 📊 Baseline (why this ordering)

Internal phase-B real-device A/B runs (186 tasks, preliminary, mixed manual judging) put end-to-end success at **RelayBench ~67% / AndroidDaily ~51% / MobileWorld ~42%**, with median task wall-clock of 75–128 s. Conclusions:

1. **Reliability is the biggest gap** — a failed leg raises and kills the whole flow, yet every ingredient recovery needs (leg verdicts, the blackboard, the MW conversion, route solidification) already exists;
2. **Latency is bottlenecked by the ~1.2 s/frame screencap and the fixed sleeps built around it**, not by the framework;
3. Memory, card scaling, and platforms come after: without ①'s success rate, everything else is pouring water into a leaking bucket.

| Phase | What | Size | Acceptance (baseline → target) |
| --- | --- | --- | --- |
| P1 | Runtime failure-recovery loop | ~3 wks | AndroidDaily 51%→70%+, MobileWorld 42%→60%+, RelayBench 67%→85% |
| P2 | Streaming capture + latency engineering | ~2 wks (parallel with P1) | Non-LLM per-step overhead ~2.5 s→<0.5 s; median task wall −~30% |
| P3 | User memory layer | ~2 wks | Halve ask_user rounds on implicit-preference tasks |
| P4 | Card regression CI + semi-automated authoring | CI 1 wk + recorder 3 wks | Card breakage detection: "when it fails in prod" → auto-issue within 24 h |
| P5 | Platforms / OEM | ongoing | milestone-based (below) |

---

## ♻️ P1 Runtime failure-recovery loop (~3 weeks)

> **Status (2026-07-08)**: R0–R3 shipped (`agents/flow/leg_recovery.py`, nl_flow §6.1);
> a mini-eval exercised the full four-tier ladder end-to-end on device, flipping one
> historical failure. R4 telemetry shipped: `recovery.json` carries per-attempt token cost;
> `run_benchmark_test.py --recovery` lands a per-row `recovery` block and
> `summary.json`/`summary.md` report first-try vs final success, per-tier hit rate
> and recovery-token inflation (pinned by `tests/test_benchmark_recovery.py`).
> The formal R4 evaluation (~30 previously-failed tasks per benchmark, on/off) is still pending.

**Before P1**: in `flow_runner._run_app_step`, any leg failure (rc≠0 / a bind with no captured reply / the output-free completion assert / a failed leg-judge verdict) raised and terminated the flow.

### R0 Failure taxonomy (days 1–2)

Recovery strategy depends on why the leg failed. At the point where hard signals and the leg judge converge, emit a structured `failure_kind`:

- `env_fail` — device/IME/adb layer → **never retry**; stop and report a device problem;
- `route_fail` — judge says "answered the wrong thing / landed in the wrong feature" → one reworded retry first (cheaper than rerouting), then reroute;
- `app_fail` — entry path was right but the app side didn't finish (risk-control wall / timeout / crash) → start at the retry tier;
- `judge_uncertain` — a reply exists but confidence is low → one retry, no escalation.

Lands as one extra field in `leg_verdict.json` + `summary.json`; the classification rides the existing leg-judge call — no new LLM round-trips.

### R1 Retry tier (days 3–5)

Same app, same capability, from scratch: force-stop (reuse pre-kill) → fresh conversation → for `route_fail/judge_uncertain`, one cheap LLM call first to reword the submit prompt (input = original prompt + judge's failure reason + truncated bad reply). **`prompt_template` cards may only re-extract slots — never reword the template** (fixed wording is the whole point of that mechanism).

**Budget guardrails (global to P1)**: ≤1 retry per leg, ≤2 recovery legs per flow, a token ceiling for recovery (default 15k); `RELAY_RECOVERY=0` disables the whole ladder and restores today's behavior (benchmark comparability depends on this switch).

**Safety red line (ships with R1, not later)**: for any capability with `handoff_to_user_required: true`, a retry is only allowed after confirming the previous run never crossed the handoff point (check the trailing action in the traj); when in doubt, don't retry — emit the partial-success report. Recovery must never produce a double order.

### R2 Reroute tier (days 6–9)

Add `exclude: [(app_id, capability_id)]` to the three-stage router (enforced in both stage-1 prefilter and stage-2 rerank) and reroute the leg with the failed pair excluded; if no second-best card exists, escalate to R3. Feed the failure to the route overlay as a negative signal (it already consumes leg verdicts).

### R3 Partial replan + runtime MW fallback (days 10–13)

- `FlowPlanner.replan_tail(plan, failed_step, blackboard, failure_summary)`: re-plan only the steps after the failure point, injecting completed legs' bind outputs as established facts;
- open the plan-time "unsatisfiable → MW leg" conversion to runtime: a leg that also failed R2 becomes a `type: mobileworld` leg, its answer fed back into the blackboard (the machinery already lives in `flow_planner_mw`);
- **terminal state**: when the ladder is exhausted, don't raise — emit a partial-success report: which legs succeeded, what the blackboard holds, where it stalled, and how the user can take over.

### R4 Telemetry + evaluation (days 14–18)

- Every recovery attempt writes `recovery.json` (tier / reason / cost / outcome) into the leg dir; `plan_summary` gains first-try vs. final success columns;
- rerun ~30 previously-failed tasks per benchmark (`--ids-file`); report success uplift, per-tier hit rate, and token inflation; **cut any tier with <10% hit rate** — the ladder is not better for being longer;
- device-less tests: a fault-injection wrapper around `InProcessLegExecutor` (call n returns a chosen `failure_kind`) pins the escalation logic and the guardrails.

---

## ⚡ P2 Streaming capture + latency engineering (~2 weeks, parallel with P1 — P1 lives in the flow layer, P2 in the device layer)

> **Status (2026-07-08)**: S1 shipped (`agents/device/android_stream.py`,
> `RELAY_CAPTURE_BACKEND=scrcpy`; default screencap unchanged; PyAV via the
> optional `stream` extra). Measured on a Pixel 9: exec-out ~2.0 s/frame →
> **~8 ms/frame** steady-state (first frame incl. startup ~1.3 s), identical
> resolution, content diff 0.8/255; a Tongyi QA task ran end-to-end on stream
> frames with zero fallbacks. S2 shipped: the `DeviceBackend.wait_settled` seam
> (default False = fixed sleeps unchanged); on the scrcpy stream, "no new frame
> for one quiet window" = settled (the encoder only emits on change — no pixel
> diffing) replaces all four fixed sleeps (step_wait / wait action / blind-step /
> poll-skip), worst case spending the original budget; `RELAY_SETTLE_DETECT`(1) /
> `RELAY_SETTLE_QUIET`(0.2 s). Measured: static-screen step settle 0.5 s→0.2 s,
> swipe animations correctly waited out. S3 (n=3 equivalence + 30-task
> wall-clock) still open.

**Today**: screencap measures ~1.2 s/frame and is the dominant per-step cost; the fixed sleeps (step_wait 0.5 s / blind-step 0.15 s / poll-skip 0.3 s) exist precisely because frames are too expensive to poll.

### S1 scrcpy streaming backend (days 1–4)

- New `agents/device/android_stream.py`: push scrcpy-server → start via `app_process` → adb forward the H.264 socket → PyAV decode, with a resident thread keeping a latest-frame buffer; `screencap()` becomes a buffer read (milliseconds);
- hangs off the existing backend seam: `RELAY_CAPTURE_BACKEND=screencap|scrcpy` (default unchanged); startup failure logs at **warning** and falls back to screencap (repo convention: fallbacks must be loud); the scrcpy-server binary is not vendored (fetch-and-verify on first use, or documented placement);
- the on-device app is unaffected (it captures via MediaProjection) — this is host-side only.

### S2 Fixed sleeps → frame-diff settle detection (days 5–8)

With cheap frames, replace each fixed sleep with `wait_until_stable(timeout, epsilon)` (return as soon as two consecutive frame hashes differ below threshold), one site at a time with a smoke run after each. `wait_for_reply`'s stage-1 precheck benefits for free. Animations really take 0.3–0.8 s while fixed sleeps assume the worst case — this is where the time comes back.

### S3 Equivalence validation + evaluation (days 9–12)

The key risk is **behavioral drift**: decoded frames differ from screencap frames in color/compression, which can affect VLM grounding and region hashing. Run the same tasks n=3 under both backends and diff action sequences and success; re-calibrate hash thresholds if needed. Then measure wall-clock on ~30 phase-B tasks. **Honest expectation**: the in-app agent's own reply latency (~18 s per reply) is not ours to cut; the task-level target is −~30%, not an order of magnitude.

---

## 🧠 P3 User memory layer (~2 weeks)

> **Status (2026-07-08)**: M1–M4 shipped (`agents/flow/user_profile.py`, schema
> `spec/profile.schema.json`, unit tests `tests/test_user_profile.py`).
> M1: `${RELAY_PROFILE_ROOT:-~/.relayagent}/profile.yaml` (`RELAY_PROFILE=0`
> turns the layer off; a malformed file degrades to "no profile" with a warning).
> M2: ① profile summary on the synthesis prompt (verified on device: "navigate
> home" plans straight to `导航去<home address>`, zero ask_user rounds);
> ② profile values as `prompt_template` slot candidates; ③ ask_user select_from
> pre-selects the previous choice (`last_choices` records the user's own explicit
> pick automatically). M3: post-flow one-call preference proposal → **asks y/n
> before writing** (EOF/batch = declined); benchmarks force `RELAY_PROFILE=0`
> (token fairness + reproducibility). M4: `RELAY_TRAJ_REDACT=1` replaces profile
> values with `<profile:section.key>` at every log-write site (agent/flow
> llm_calls, steps.json, summary.json, flow_report.json, the agent_reply.json log
> copy); an on-device leak scan found zero plaintext. The 10-task
> implicit-preference acceptance comparison is still to run. Caveat: resolved
> profile values are baked into cached plans (`manifests/_generated/`,
> gitignored) — after changing the profile, bypass stale cache with `--no-cache`.

**Principles**: local, explicit, inspectable/deletable. The project's premise is that context already lives inside the apps; the memory layer only fills in preferences the user didn't spell out — no scraping.

- **M1 Profile store** (2 days): `~/.relayagent/profile.yaml` (filesDir on Android, redirected the same way as `RELAY_TRAJ_ROOT`): address book, dietary preferences, contact aliases, per-app hints. Schema goes into `spec/`, validated.
- **M2 Injection points** (4 days): ① profile summary attached to the `FlowPlanner` synthesis prompt ("send it home" resolves without an ask_user round); ② profile values as candidates during `prompt_template` slot extraction; ③ `ask_user` `select_from` pre-selects the previous choice.
- **M3 Memory writes** (3 days): after a successful flow, one cheap LLM call decides whether a stable, memorable preference appeared — then **ask before writing** (a y/n). Never write silently.
- **M4 Privacy plumbing** (2 days): profile values ride prompts into traj logs → `RELAY_TRAJ_REDACT=1` replaces them with `<profile:home_address>`-style placeholders before landing. Without this, sharing a traj for debugging leaks a home address.
- **Acceptance**: 10 hand-built implicit-preference tasks ("send home" / "the usual" / "that place from last time"); compare ask_user rounds and success with/without a profile; leak-scan the trajs for profile plaintext.

---

## 🃏 P4 Card regression CI + semi-automated authoring

- **C1 Card-health CI** (1 week, first — it protects the existing assets): nightly, per card: install check (reuse `check_device_env`) → walk the entry path and assert reachability (`native_runner --max-step`, no prompt sent, zero tokens); weekly full pass: one example_prompt per capability, assert a reply is captured. Output: a health table; two consecutive failing nights auto-file an issue (the `card_issue` template + `gh` CLI). Starting hardware = one real phone + the relay-test AVD (international apps).
- **C2 Card recorder** (3 weeks): `scripts/card_recorder.py` — a human walks the path into the in-app agent once on a real device; the recorder listens to the a11y event stream, translates the tap sequence into draft entry-path selectors, sends probe prompts to classify capabilities, and emits a YAML draft with a `provenance` skeleton → human revision → PR. Cuts card authoring from a day to an hour — the lever that makes community contribution scale.
- **C3 Fully-automatic discovery** (unscheduled): an exploration agent that finds AI entry points on its own; revisit once C2 has accumulated data on what entry points look like.

---

## 🌍 P5 Platforms / OEM (ongoing, milestone-based)

- **H1**: bring the HarmonyOS app (`harmony/`) to feature parity with `android/`; hdc backend from skeleton to usable;
- **H2**: iOS WDA backend running one international card end-to-end (Booking / Copilot have iOS builds; the `app_ids` multi-platform mapping already exists);
- **H3**: OEM conversations — the ammunition is the tech report's 11–19× token numbers plus SPEC §14's A2A forward-compatibility story ("cards today; when you ship an endpoint the card degrades into a thin shim"). **After P1**: 42–67% success doesn't get through an OEM's door; 80%+ does.

---

## 📅 Sequencing and global discipline

```
wk 1-3   P1 recovery loop (R0→R4)
wk 1-2   P2 streaming capture (parallel, S1→S3)
wk 4     full phase-B rerun with P1+P2 merged (the numbers double as OEM/paper ammunition)
wk 5-6   P3 memory layer
wk 5     P4-C1 card CI (small, fits in gaps)
wk 7-9   P4-C2 recorder
ongoing  P5 milestones
```

Two rules that apply to every phase:

1. **Every phase ends with a phase-B subset rerun**, numbers land in `report/` — the project's credibility rests on "every claim has n=3 data"; productization doesn't get to drop that;
2. **Every new behavior ships behind an env switch defaulting to today's behavior** (`RELAY_RECOVERY` / `RELAY_CAPTURE_BACKEND` / `RELAY_PROFILE` / `RELAY_TRAJ_REDACT`), so a comparable baseline is always one flag away.
