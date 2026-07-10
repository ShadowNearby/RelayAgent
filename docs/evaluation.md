<h1 align="center">RelayAgent Evaluation Design</h1>

<p align="center">
  <b>How RelayAgent is evaluated: benchmarks, baseline, metrics, and the fairness protocol</b>
</p>

<p align="center">
  <b>English</b> | <a href="evaluation.zh.md">中文</a>
</p>

> The Chinese version ([`evaluation.zh.md`](evaluation.zh.md)) is authoritative when the two diverge.
> Companion code: `scripts/run_benchmark_test.py` (A/B driver + plan-only), `scripts/eval/plot_eval_figs.py` (figures).

## 🎯 1. One sentence

On the **same physical device, with the same unified VLM judge**, run the same task set through both **RelayAgent (relay)** and **MobileWorld `general_e2e` (baseline)**, and compare **success rate / wall-clock / tokens / steps**, stratified by **app coverage (covered / fallback)**. Core claim: **on tasks where RA has a specialized in-app agent it saves large amounts of time and tokens with success no worse than the baseline; where coverage is missing it degrades to the baseline and only pays a small planning tax.**

## 📏 2. Baseline

- Primary baseline: **MobileWorld `general_e2e`** (a general GUI agent, frame-by-frame pixel grinding) — represents the "no specialized routing, pure visual operation" paradigm.
- Relationship clarification (important): **the baseline is a subset of RA only in the fallback tier, not of RA as a whole**.
  - **mw_fallback tier**: RA converts unsatisfiable legs to `type: mobileworld` and hands them to the **same** general_e2e → in this tier the baseline is a subset of RA.
  - **covered tier**: the executor is swapped for a specialized in-app agent (Qwen / Amap / Ctrip / …) — a **different path (replacement, not containment)** from general_e2e.
  - Corollary: "RA ≥ MW" on success is an **empirical expectation, not a logical guarantee** — the covered tier uses a different executor and may be worse per task; the fallback tier additionally loses to **routing / handoff errors**.
- Planned ablations: `RELAY_SCRAPE=0` / a11y-only agent, isolating how much of the gain comes from routing vs. reply scraping.

## 🧪 3. Three benchmarks (co-equal, no primary/secondary)

| Benchmark | Source | Size | Lang | Nature / role |
| --- | --- | --- | --- | --- |
| **RelayBench** | in-house `benchmark/relaybench_tasks.yaml` | 30 (15 single + 15 cross) | zh | Covers RA's 10 manifest apps; precise internal measurement; risk = self-built, open to cherry-pick suspicion |
| **AndroidDaily** | HF `stepfun-ai/AndroidDaily` (`Android Daily.csv`) | 235 (mostly single-app) | zh | External standard, 30+ everyday Chinese apps, heavily hits RA coverage (Ctrip/Amap/Taobao/Eleme/WeChat/XHS…). **Strong external evidence** |
| **MobileWorld** | HF `Tongyi-MAI/MobileWorld` | 201 → **161 (after `--skip-mcp`)** | mostly en | External standard, Mail/Mastodon/Files/Calendar/… — low RA coverage (only Maps/Amap ≈ 9). Role = generalization / non-degradation + cross-app stress |

- **MCP skip**: MobileWorld tasks touching `MCP-*` (Amap/arXiv/Github/stockstar/jina) are tool-calls, not real GUI; **all are cross-app (no pure-MCP tasks)**. `--skip-mcp` drops 40 → 161 tasks (85 cross + 76 single; 144 en / 17 cn).
- **AndroidDaily metric mismatch**: its native metric is **step-action-accuracy** against ground-truth trajectories; RA routes to an in-app agent and produces no comparable step sequence → **we reuse only its task instructions and score with the unified end-to-end VLM judge** (same yardstick for both systems).

## 🪜 4. Core axis: covered vs fallback

Instead of ranking benchmarks, **stratify within each benchmark** (criterion: the **kind** of each leg in the planner output — `specialized` true vertical capability / `foundation` generic `foundation_llm` / `mw` MobileWorld fallback leg):

- **covered**: **every** leg is `specialized` (routed to a specialized in-app agent) → **the headline time/token savings live here**.
- **foundation_fallback**: no MW leg, but ≥1 `foundation_llm` leg (only generic QA matched).
- **mw**: **every** leg is MobileWorld fallback (== the baseline substrate) → RA degrades to the baseline; the story is **non-degradation + planning tax**.
- **mixed**: MW legs mixed with non-MW legs. `plan_summary.json["mw_fallback"]` reports MW share for this tier (and globally): task-level `task_touch_rate`, leg-level `mw_leg_rate`, per-mixed-task `mixed_task_mw_ratios`.
- **invalid / error**: illegal plan / network etc. (with MW fallback enabled, unsatisfiable / repair-exhausted plans convert to MW, so invalid is now nearly empty).

RA's 10 hand-written manifests: Qwen, Amap, Ctrip, WeChat, RedNote, WPS, Booking, Reddit, Gemini, Copilot. **The gains concentrate in these apps** — the covered stratification deliberately exposes where the gains come from, closing the cherry-pick objection.

## 📐 5. Metrics

1. **Completion rate** (unified VLM judge `agents/leg_judge`, SUCCESS/total) — overall + per tier.
2. **Wall-clock** (whole-task subprocess wall time) — **three accountings reported side by side** (closes selection bias): all tasks / each system's completed-only / **intersection where both systems succeed (paired)**. The intersection is the headline efficiency number (per-task RA/baseline ratio on the same task), but it **must not be reported alone** — conditioning on baseline success deletes exactly the cases where MW times out and RA finishes in a few steps (RA's biggest wins). It must sit next to the all-tasks accounting (which contains MW timeout ceilings and overestimates RA in the opposite direction). See §9 fig6/fig7, fig5.
3. **Tokens** (prompt/completion/total) — same accountings as wall-clock.
4. **Steps / LLM call count** — the covered tier best illustrates "one submit replaces dozens of taps".
5. **Cross-app handoff success rate** (reported separately; RA's differentiating capability).
6. **Failure attribution** (timeout / mis-route / grounding error / handoff false stop).

## 💰 6. Per-tier expectations for time/tokens (present honestly)

- **covered tier**: planning tax + one cheap in-app submit ≪ MW's dozens of frame-by-frame steps → **RA wins big**.
- **mw_fallback tier**: RA = planning overhead (+ coverage-gap repair rounds) + **the same execution as MW** → **RA slightly slower, slightly more tokens, success ≈ MW**. This tier is RA paying a net planning tax — **plotting it honestly is the most convincing** (see §9 Fig.5).

## 🔍 7. Protocol / honesty items (must be disclosed in the paper)

1. **Self-judging**: the judge is RA's own leg_judge → manually verify a 30–50 task subsample and report agreement.
2. **Relay token accounting**: relay total tokens read the authoritative `<flow_root>/token_usage.json` written by `run_plan.py`; `total` **includes the plan-synthesis phase** (+ repair rounds), `by_phase` splits plan/flow/agent, so the planning tax is directly quantifiable. **Per-call logs aligned on both sides**: each results.jsonl row's `llm_calls` holds per-call metrics (tokens + latency + model + purpose); full bodies are persisted — relay in each leg's `traj.json`, mw via the non-invasive probe `agents.llm.mw_llm_probe` writing `<sys>/user_task/llm_calls.json`.
3. **Completed-only bias**: `_aggregate` currently aggregates time/tokens over each system's own completed tasks → must be co-reported with all-tasks **+ the both-success paired intersection**. The three accountings are biased in opposite directions (completed-only: each system on its own set, unpaired; all-tasks: contains MW timeout ceilings → overestimates RA; intersection: conditions on baseline success → deletes RA's biggest wins → underestimates RA), so only all three together are honest. The paired intersection is computed as a per-`task_id` join of both-success tasks with per-task ratios (not a ratio of means).
4. **Fairness switches at test time**: see §8.

## 🎚️ 8. Switches forced off during tests (fairness + clean wall-clock)

`run_benchmark_test.py` writes `os.environ` after arg-parse; both the relay subprocess and the in-process plan-only planner inherit:

| Switch | Default | Why off for tests | Re-enable |
| --- | --- | --- | --- |
| `RELAY_ROUTE_OVERLAY` | **0 (off)** | Route solidification short-circuits the planner with 0-LLM table lookups for later tasks, leaking warm state across tasks → tokens/time drift with task order, unfair | `--route-overlay` |
| `RELAY_STEP_LOG` | **0 (off)** | Writes a PNG per step + re-encodes an annotated frame for tap/swipe, polluting the wall-clock; traj.json action trajectory still kept | `--step-log` |
| `RELAY_CAPTURE_FULL_REPLY` | **0 (off)** | The MW baseline `general_e2e` has **no scrolling capture** — once the reply looks stable it reads the currently-visible frame text and `answer`s; RA's `x_capture_full_reply`/`capture_full` scrolls offscreen reply cards into view and stitches them — strictly more reply content for the same goal → unfair. When off, `wait_for_reply` returns the first visible frame text right after the text-hash-stable done call, skipping the scrolling capture phase (gated in `relay_agent._materialize`: `capture_full = p["capture_full"] and self.capture_full_enabled`) | `--full-reply` |
| plan/route cache | **off** (relay runs `--no-cache`) | otherwise a warm plan is reused, inflating time savings | — |
| `--record` screen recording | unused | recorder backend overhead | — |

> Note: the overlay is a **real RA efficiency feature**. It defaults off for fair per-task comparison; how many planning calls it saves should be measured in a separate **overlay on/off ablation** with a warmed-up overlay table.
> Keep enabled (correctness-related, **do not disable**): `RELAY_FRESH_CONV`, the AdbKeyboard IME, cold-launch.

## 🖼️ 9. Figure set

Code: `scripts/eval/plot_eval_figs.py`, output `docs/eval_figs/{png,pdf}`. The data block at the top of the script is swapped for the benchmark outputs (`summary.json` / `plan_summary.json`) as runs complete. Fixed palette: relay blue `#0072B2` / baseline orange `#D55E00`, covered dark green, fallback light green/purple.

- **Fig.1 coverage stratification**: one horizontal stacked bar per benchmark (covered/foundation/mixed/mw/invalid), N on the right. Data ← each `plan_summary.json["by_tier"]`.
- **Fig.2 covered-tier efficiency**: relay vs baseline time/token/steps triptych on the covered tier, `n×` saving on top of each bar, success% gate at the bottom. Data ← `summary.json` (covered-subset aggregate).
- **Fig.4 per-app dumbbell**: relay vs baseline success/time/token per app; covered apps green and bold on top, fallback apps gray below (two points coincide); log axis for time/token. Data ← `summary.json["by_app"]`.
- **Fig.6 / Fig.7 paired scatter (both-success intersection)**: `fig6_paired_tokens` / `fig7_paired_time`, one subpanel per benchmark (ratio ordering only makes sense within one scale). Only both-success tasks; x is unlabeled, sorted by `baseline/RA` ratio descending (left = RA wins most); each task draws blue (RA) + orange (baseline) points on the same x joined by a thin vertical line (**gray = RA cheaper = covered win; red = RA dearer = fallback planning tax** — §6's two-tier expectation drawn directly into the scatter); log Y; median n× and RA-wins% in the top right. Covered share follows each benchmark's plan-only covered_rate (RelayBench high → almost all gray, almost all wins; MobileWorld low → mostly red, median ≈ 0.9×). This is the scatter substrate of §5.2's intersection accounting — more trustworthy than fig2's mean n× (distribution and counterexamples visible). Data ← per-task join (`ra_ok/base_ok/ra_t/base_t/ra_k/base_k`).
- **Fig.5 (outcome matrix table) `fig5_outcome_table`**: 2×2 success outcome (RA × baseline), one row per benchmark + TOTAL; four cells both-succeed / `RA✓ base✗` / `base✓ RA✗` / both-fail (+ N), colored green/blue/orange/red. The two off-diagonal cells are discordant pairs → feeds a **McNemar significance test** directly. Data ← same per-task join as fig6/7.
Layout rules: success and efficiency always share a panel; the three benchmarks reuse one isomorphic figure; report median + distribution, not just means; time/tokens always in both accountings; one palette across the paper.

## 🗺️ 10. Driver implementation map (`scripts/run_benchmark_test.py`)

- `BENCHMARKS`: `mobileworld` / `relaybench` / `androiddaily` (loader + smoke picker; `single_app` removed).
- `--skip-mcp`: `_touches_mcp` filters tasks touching `MCP-*`.
- `--plan-only`: pure LLM, no device; tiers by leg kind (covered / foundation_fallback / mw / mixed); outputs `plan_summary.json` (`by_tier` / `covered_rate` / `mw_fallback`{task+leg MW share} / `covered_app_hits` / `covered_capability_hits`).
- `_aggregate` (per system) + `_aggregate_by_app` (per app×system, feeds Fig.4) → `summary.json`'s `by_system` / `by_app`.
- Unified judge: `_judge` calls `leg_judge.judge_leg`; `loading` triggers one re-capture.

## 📊 11. Coverage data

**Plan-only tier classification** — by leg kind (specialized / foundation / mw), see §4:

| Benchmark | n | covered | foundation_fallback | mw | mixed | covered_rate | MW task share | MW leg share |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| RelayBench | 30 | 27 | 3 | 0 | 0 | **0.90** | 0% | 0% |
| MobileWorld (skip-mcp) | 161 | 61 | 10 | 90 | 0 | **0.379** | 55.9% | 39.8% |
| AndroidDaily | 235 | 71 | 19 | 143 | 2 | **0.302** | 61.7% | 56.4% |

MobileWorld's large mw tier is dominated by device/OS actions a chat assistant cannot perform (the router's stage-3 escape hatch degrades them to the MW fallback); its covered 61 includes Gemini mail/calendar/SMS (declared in the manifest, sole provider in the matrix). AndroidDaily's mw tier comes from the many apps without manifests (Didi / JD / Meituan / Pinduoduo / Bilibili…). The real-device A/B runs on the covered union of the three benchmarks (27 + 71 + 61 = **159 tasks**).

## 🚧 12. Open items

- Overlay on/off ablation: how many planning calls route solidification saves (needs a warmed-up table, see §8).
- Model-sensitivity ablation: rerun a few baseline-success cases with a weaker model to quantify dependence on base-model quality.
- Self-judge agreement: manual verification on a 30–50 task subsample (§7.1).

## 🔗 Related docs

- Cross-app flow architecture: [`docs/nl_flow.md`](nl_flow.md); capability matrix (source of truth): `docs/app_capability_matrix.csv`.
- Route solidification overlay: `nl_flow.md` §9.
