# RelayAgent

<p align="center">
  <a href="report/RelayAgent-TechReport.md">Tech Report</a> •
  <a href="SPEC.md">Spec (v0.1)</a> •
  <a href="docs/roadmap.md">Roadmap</a> •
  <a href="CONTRIBUTING.md">Contributing</a> •
  <a href="https://github.com/ShadowNearby/RelayAgent/issues">Issues</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License">
  <img src="https://img.shields.io/badge/Python-3.12-blue.svg" alt="Python 3.12">
  <img src="https://img.shields.io/badge/SPEC-v0.1-green.svg" alt="SPEC v0.1">
  <img src="https://img.shields.io/badge/PRs-welcome-red.svg" alt="PRs welcome">
</p>

**A discovery layer for delegating a user's request to the AI agent that already lives inside a mobile app** — so that an OS-level agent (HarmonyOS 小艺, Apple Intelligence, …) can hand a task to the in-app agent that already holds the user's account, address, and payment context, instead of re-driving the whole UI itself or waiting for the vendor to publish an endpoint.

One machine-readable **card** per app. GUI-mediated by default. Vendor-cooperation-*optional*.

<p align="center">
  <img src="assets/paper/arch.png" alt="RelayAgent overview: a natural-language request is decomposed into subtasks, each delegated to a suitable in-app assistant" width="820">
  <br>
  <em>One request, many in-app assistants: the planner decomposes the instruction and delegates each subtask to the app agent that already has the user's context.</em>
</p>

> **Status:** early but measured. SPEC v0.1, ten verified Android reference cards (50 declared capabilities), a native Android relay adapter, an NL cross-app planner with runtime failure recovery and coverage fallbacks, an on-device Android app, and real-device benchmarks against a pure-GUI baseline. Full method and numbers: [**Tech Report**](report/RelayAgent-TechReport.md). Contributors welcome.

## 📢 Updates

- **2026-07-08 — Roadmap P1–P3 shipped.** (1) *Runtime failure recovery*: a failed app leg is classified (`env_fail`/`route_fail`/`app_fail`) and climbs a ladder — retry with LLM re-phrasing → reroute to another app → runtime fallback → partial-success report — with per-attempt token telemetry in the benchmark harness ([nl_flow §6.1](docs/nl_flow.md)). (2) *Streaming capture*: `RELAY_CAPTURE_BACKEND=scrcpy` replaces the ~1.5 s `screencap` with a persistent H.264 stream (**~8 ms/frame** on a Pixel 9), and frame-arrival settle detection replaces fixed sleeps. (3) *User memory*: an on-disk profile is injected into planning and slot extraction ("navigate home" resolves to the stored address), with ask-first preference writes and trajectory redaction.
- **2026-07-07 — On-device Android app milestone.** The full NL flow now runs inside a standalone app ([`android/`](android/README.en.md), accessibility-service + Chaquopy-embedded Python) — **no computer, no adb**: chat-style task thread, live run cards, structured run-log viewers, zh locale + dark theme; instrumented suite verified on a Pixel 9. Also published the [productization roadmap](docs/roadmap.md).
- **2026-06-14 — Repo re-org + CI.** `agents/` split into functional subpackages (`device/`, `llm/`, `runtime/`, `routing/`, `agent/`, `flow/`), GitHub CI, optional extras (`dev`/`mw`/`stream`), manifest + device validation tooling.

## 📋 Table of contents

- [The third path](#the-third-path)
- [How it works — three pieces](#how-it-works--three-pieces)
- [Beyond a single card: the cross-app flow](#beyond-a-single-card-the-cross-app-flow)
- [Demo](#demo)
- [Results](#results)
- [What's in the repo](#whats-in-the-repo)
- [Run it (real-device, multi-VLM)](#run-it-real-device-multi-vlm)
- [Run tests](#run-tests)
- [Documentation](#documentation)
- [MVP scope (v0.1)](#mvp-scope-v01)
- [Known blockers](#known-blockers)
- [What this project is *not*](#what-this-project-is-not)
- [Getting involved](#getting-involved)

## The third path

An OS-level agent that wants to *act inside* a third-party super-app has had two options, both of which break down in the open ecosystem:

- **Vendor-cooperation APIs** — A2A, Apple App Intents / Shortcuts, Android AppFunctions, HarmonyOS HMAF. Clean and typed, but gated on the *vendor* publishing an endpoint. The long tail — and most Chinese super-apps — never do.
- **Pure GUI agents** — Mobile-Agent, MobiAgent, AppAgent, AutoGLM, … drive the full UI from screenshots, step by step. Brittle, slow, detectable, legally gray, and *wasteful*: they re-derive login, location, and payment state the app already has.

The observation behind RelayAgent: most super-apps **already ship their own logged-in AI agent** — Amap's assistant tab, Yuanbao in WeChat, Taobao/闪购's shopping assistant, Xiaohongshu's 点点, WPS AI. For a large fraction of real intents, the capability the user wants *already exists, already logged in, already holding the user's context* inside the app.

So we argue for a **third path: delegate the user's intent to the app's own embedded agent** — rather than re-implementing the task (pure GUI agents) or waiting for an endpoint (A2A). What that needs is not a smarter automation model but a *contract*: a per-app discovery layer telling the OS agent which apps embed an agent, where its input box is, what it can do, and — critically — **when control must return to the user.**

The same navigation task, side by side:

<p align="center">
  <img src="assets/paper/gui-agent.png" alt="A pure GUI agent: one screenshot + one VLM round-trip per step" width="820">
  <br>
  <em><b>Pure GUI agent</b> — every step is a screenshot + VLM round-trip: find the search box, type, pick the result, tap Start… dozens of vision calls per task.</em>
</p>

<p align="center">
  <img src="assets/paper/dele-agent.png" alt="RelayAgent delegation: the card supplies a deterministic entry script; the in-app assistant does the task" width="820">
  <br>
  <em><b>Delegation via a card</b> — the manifest supplies a deterministic entry script (open app → type the templated prompt → submit); the in-app assistant drives the actual task with the user's own context.</em>
</p>

| | Vendor APIs<br>(A2A / App Intents / HMAF) | Pure GUI agents<br>(Mobile-Agent, MobiAgent, …) | **RelayAgent (ours)** |
| --- | --- | --- | --- |
| Who does the task | the app's exposed API | OS agent re-drives the full UI | the app's **own logged-in** in-app agent |
| Vendor cooperation | **required** | none | **optional** |
| User context (login, address, payment) | re-provided | re-navigated each run | **already present** |
| Irreversible-action safety | API-level | ad-hoc | **`handoff_to_user_required` contract** |
| Cost predictability | high | **low** (high run-to-run variance) | **high** (near-identical) |

A neighbor is emerging fast in 2026: vendors wiring their *own* assistant into their *own* services (Alibaba's Qwen app has absorbed Taobao 闪购, Fliggy, and Amap ride-hailing). This **validates the premise** — in-app assistants really do complete real, logged-in transactions — and **bounds our niche**: such integration is first-party and intra-ecosystem. RelayAgent targets the **cross-vendor / long-tail gap** that no super-assistant reaches. Where first-party integration *does* exist, RelayAgent defers to it. (Full positioning: Tech Report §2.)

## How it works — three pieces

1. **Discovery — the card.** A per-app YAML manifest (`manifests/*.yaml`, JSON-Schema-validated by `spec/schema.json`) describing the launcher entry path into the embedded agent, its capabilities, example prompts, latency hints, and handoff policy. *(Spec: §4.)*
2. **Access — the relay adapter.** `agents/agent/relay_agent.py` materializes a card into deterministic device actions over direct adb: cold-launch → walk the entry path → type the prompt → wait for the reply → scrape it → hand off. Accessibility-tree-first, provider-agnostic across VLMs. *(§5.)*
3. **Safety — the handoff contract.** A capability marked `handoff_to_user_required: true` *must* emit an `ask_user` and return control **before any irreversible action** — payment, ride confirmation, order submission. The in-app agent does the reversible preparation; the human authorizes the irreversible step. *(§4.1.)*

### A card in use

```
user: "Call an economy car to the airport"
        │
        ▼
OS-level agent
  1. receives an explicit target app, e.g. com.autonavi.minimap
  2. matches the request against the card's capabilities → picks `hail_ride`
  3. follows card.entry → opens Amap, taps the AI tab, flips it to text mode
  4. types the user's original prompt into the chat input
  5. honors `handoff_to_user_required: true` — returns control before the 立即打车 CTA
        │
        ▼
Amap's in-app assistant does the actual work (it already knows the user)
```

The target app is **explicit**. The OS agent selects a capability within it. The in-app agent acts. The card is the contract.

## Beyond a single card: the cross-app flow

A single card answers "how do I hand *this* app *this* task". Everything that has landed since v0.1 turns that primitive into an agent architecture — decompose, delegate, orchestrate:

- **NL planning + three-stage routing.** `scripts/run_plan.py` synthesizes a multi-app flow from one natural-language request; each step is routed (non-foundation prefilter → rerank → foundation fallback as a separate stage) against the [capability matrix](docs/app_capability_matrix.csv). Blackboard state carries results between legs. ([NL flow architecture](docs/nl_flow.md))
- **Runtime failure recovery** *(roadmap P1, default on)*. A failed leg is classified, then climbs: retry (with LLM re-phrasing for route failures) → reroute to another app → runtime fallback → partial-success report (`flow_report.json`). Handoff-gated capabilities only ever retry — never reroute past the safety contract. ([nl_flow §6.1](docs/nl_flow.md))
- **Coverage fallbacks.** When no card covers a leg — or recovery runs out of rungs — the leg is handed to a manifest-free general GUI agent on the same runtime (or to MobileWorld's `general_e2e` when installed), instead of giving up. Priority: MW > general > unsatisfiable. ([nl_flow §10](docs/nl_flow.md))
- **Streaming capture + settle detection** *(P2)*. `RELAY_CAPTURE_BACKEND=scrcpy` turns the ~1.5 s per-step `screencap` into an ~8 ms read of the latest decoded frame, and "no new frame within a quiet window" replaces fixed sleeps. Any failure permanently falls back to `screencap` — no restart storms.
- **User memory** *(P3, default on; no-op without a profile)*. A YAML profile (`spec/profile.schema.json`) is injected into planning and template slot extraction; preferences are written only after an explicit y/n; `RELAY_TRAJ_REDACT=1` masks profile values in every log sink.
- **Route solidification.** Verdict-backed routes solidify into 0-LLM table lookups on repeat requests. ([nl_flow §9](docs/nl_flow.md))
- **On-device app.** The whole pipeline — routing, planning, leg execution, logging — runs inside a standalone Android app via an accessibility service and embedded Python: no computer, no adb. ([`android/`](android/README.en.md))
- **Platform seams.** Device I/O goes through a backend abstraction (`agents/device/`; Android = direct adb, iOS/HarmonyOS skeletons), and manifests declare `platforms` / per-platform `app_ids`. ([device backends](docs/device_backends.md))

## Demo

One real-device end-to-end run:

**T1 — single-app order.** *"帮我点三杯蜜雪冰城蜜桃四季春，温度和糖度都用默认"* → the 千问 (Qwen) in-app assistant assembles a 3-cup cart and stops at the payment screen for the user to confirm.

![Order food via the in-app assistant](assets/RelayAgentDemoOrder/RelayAgentDemoOrder.gif)

## Results

### What delegation buys on one task (tech report)

A four-configuration A/B on a real device (Tech Report §8) isolates *what delegation buys* on **T1**, a single-app order for three Mixue drinks. All configs place the same order through the same backend (Taobao 闪购's assistant *is* 千问), varying only interaction style. Median tokens, n=3 for the RelayAgent / pure-VLM configs:

**T1 — order_food**

| Configuration | Median tokens | vs RA optimized |
| --- | ---: | ---: |
| Pure-VLM, hand-driving native UI | 75 463 | 18.9× |
| Pure-VLM, *using* the in-app assistant (`general_e2e`) | 77 347 | 19.4× |
| RelayAgent, optimizations off (baseline) | 9 585 | 2.4× |
| **RelayAgent, optimized** | **3 986** | **1×** |

Reading the gradients:

- **Structured delegation (RelayAgent)** wins against a pure-VLM agent driving the *same* assistant because it removes per-step VLM re-driving. The gap is concretely **~1 screenshot vs ~30** (the cost is ~97–99% image-prompt tokens). A manifest-free delegation relay lands in between, attributing the gap to **mostly delegation, manifest secondary** (§8.9).
- **Two app-agnostic optimizations** (a two-stage reply-completion precheck + an a11y-scrape-first text path) add a further **2.4×** over the un-optimized delegation baseline (§7).

**Predictability is itself a result.** RelayAgent's per-task cost is nearly constant — T1 **3987 / 3986 / 3950** tokens (VLM calls fixed at 2) — while the pure-VLM agent varied **38k → 97k tokens at 46 → 379 s** on the identical task (all three reaching the same pre-payment screen), with premature-exit and runaway-loop tails seen in earlier exploration. A predictable ~4k beats a several-fold spread for anyone paying per token. Restated in dollars (§8.2), RelayAgent optimized is ~**$0.001/task** vs. ~**$0.016** for the pure-VLM agent (16.6×).

**Safety held.** Every `handoff_to_user_required` run stopped before the irreversible CTA — the food order at `立即支付` — with zero confirm taps. In the frozen benchmark catalog, **28/28 capabilities** across 7 cards reached their expected terminal state (§8.2.1); the current manifest catalog has since grown to 10 cards, with Reddit Ask, Booking.com AI Chat, and Microsoft Copilot verified separately.

> Numbers are the 2026-06-02 n=3 re-run; full method, threats-to-validity, and frozen data are in the [Tech Report](report/RelayAgent-TechReport.md) and `report/benchmark-data-n3.md`.

### Delegation vs. a GUI agent across three benchmarks (paper eval)

The full-system evaluation (paper preprint, arXiv release upcoming) runs RelayAgent (**RA** = the NL flow with GUI fallback) head-to-head against a pure-GUI baseline on three real-device benchmarks — **AndroidDaily**, **MobileWorld**, and **DeleBench** (30 long-horizon daily tasks of our own), each task executed by both systems under identical device state with cold-launch resets:

<p align="center">
  <img src="assets/paper/fig1_completion_bars.png" alt="Success rate: RA 46/49/83% vs baseline GUI agent 31/34/77% on AndroidDaily / MobileWorld / DeleBench" width="640">
</p>

- **Higher success rate on all three benchmarks** — 46% / 49% / 83% vs. the GUI baseline's 31% / 34% / 77% on AndroidDaily / MobileWorld / DeleBench. Where an in-app assistant covers the task, execution is handled by the assistant rather than a fragile GUI action sequence; where none does, the GUI fallback keeps the floor.
- **1.4–2× faster end-to-end** (average 1.8×) on tasks both systems complete without fallback; when fallback *is* needed, the delegation attempt adds only ~4 s (7%) on average, because capability boundaries are modeled well enough to avoid useless delegation.
- **7–10× fewer LLM tokens** (average 8.8×) without fallback; ~10 K extra tokens (5%) when falling back:

<p align="center">
  <img src="assets/paper/fig3_paired_tokens.png" alt="Per-task token consumption, RA vs baseline, paired by task" width="820">
  <br>
  <em>Per-task tokens, paired: blue = RelayAgent (RA), orange = GUI baseline. The shaded region is tasks completed via GUI fallback — delegation wins big where an assistant covers the task, and costs almost nothing extra where it doesn't.</em>
</p>

## What's in the repo

```
RelayAgent/
├── SPEC.md                    # manifest specification (v0.1)
├── SPEC-OPEN-QUESTIONS.md     # known design questions still in flight
├── spec/                      # schema.json (manifest) + profile.schema.json (user memory)
├── manifests/                 # one YAML card per app; 10 Android cards
├── agents/                    # device backends, LLM client, runtime loop, routing, relay adapter, NL flow
├── scripts/                   # run_plan.py (NL flow), benchmark runner, validation, metrics
├── android/                   # on-device app: the full NL flow on the phone, no computer, no adb
├── benchmark/                 # task sets for the A/B benchmark
├── tests/                     # device-less unit tests (CI)
├── docs/                      # design docs — see Documentation below
├── report/                    # tech report + frozen benchmark data
├── CONTRIBUTING.md
└── LICENSE                    # Apache-2.0
```

## Run it (real-device, multi-VLM)

`agents/agent/relay_agent.py` drives RelayAgent over direct adb in an in-process
`obs → predict → execute` loop (no server). The VLM is provider-agnostic (Claude, Gemini, Qwen-VL, Kimi, …); the card supplies the deterministic entry path and handoff policy.

Requires **Python 3.12** and a Linux/WSL host with adb + a USB-debugging phone (or an emulator — see [emulator testing](docs/emulator_testing.md)) with `com.android.adbkeyboard/.AdbIME` installed. Full device prep + per-benchmark app requirements: [device setup](docs/device_setup.md); pre-flight check: `uv run python scripts/validate/check_device_env.py`.

```bash
# 1. set up the venv (run the sources directly via `uv run`, don't install the project)
uv venv --python 3.12
uv sync --no-install-project --extra dev
#   optional extras: --extra stream (scrcpy streaming capture), --extra mw (MobileWorld A/B baseline)

# 2. fill in .env (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL), then drive a goal
uv run python -m agents.runtime.native_runner com.aliyun.tongyi "帮我点三杯蜜雪冰城蜜桃四季春"
```

`agents.runtime.native_runner` loads `.env`, activates the AdbKeyboard IME, cold-launches the target app through the `agents/device/` backend layer (force-stop + monkey LAUNCHER), sets `RELAY_SKIP_OPEN_APP=1` so the planner skips its own `open_app` step, runs the in-process loop over direct adb, and forwards any extra flags (e.g. `--max-step 40`) straight to the agent. Override the LLM config with `--model` / `--base-url` / `--api-key` if not using `.env`.

`--model` is provider-agnostic — point it at any OpenAI-compatible VLM (`qwen/qwen3-vl-235b-a22b`, `anthropic/claude-sonnet-4-5`, `google/gemini-3`, …). Per task, the VLM is used sparingly (this is the source of the §8 cost numbers):

- 1 text-only LLM call to pick a capability from the card.
- For each text selector, `uiautomator dump` is tried first (precise, zero-token); a small VLM grounding call only on miss.
- `wait_for_reply` decides the reply is complete **deterministically** — the a11y-tree text hash must hold byte-identical across consecutive dumps — on a wall-clock budget (`max(5×typical_latency, 60)` s). A two-stage precheck (screenshot perceptual hash → a11y-tree text hash) skips the expensive dump while the reply is still streaming. No VLM judges doneness.
- Reply text is **scraped from the a11y dump**; a VLM is only asked to read the frame when the scrape comes up empty (WebView/canvas replies). For `x_capture_full_reply` capabilities every scroll-frame extract is a scrape too.
- Card `screen_fraction` coordinates are a last-resort fallback when the a11y tree doesn't expose the element.

Optional env vars (full list in `.env.example`):

- `RELAY_MANIFESTS=/path/to/manifests` — override the default `./manifests/`.
- `RELAY_CAPTURE_BACKEND=scrcpy` — streaming frame capture (~1.5 s → ~8 ms per frame; needs `--extra stream` + scrcpy on the host) with frame-arrival settle detection; any failure falls back to `screencap` permanently.
- `RELAY_PRECHECK=0 RELAY_SCRAPE=0` — disable the two §7 optimizations (reproduces the benchmark baseline).
- `RELAY_TIMING=1` — write a per-run `wall_clock.json`.
- `RELAY_FRESH_CONV=0` — keep the previous conversation across runs (default starts fresh).
- `RELAY_ANDROID_SERIAL=...` — pin every adb call to one device in multi-device setups.

The adapter honors `handoff_to_user_required`: for any irreversible capability it emits `ask_user` before the terminal CTA rather than auto-confirming.

### Natural-language entry point

`scripts/run_plan.py` is the NL entry point. It synthesizes a flow, resolves each app step through the shared matrix-backed router, previews the plan, then executes with `--yes` — with runtime failure recovery and coverage fallbacks on by default (`RELAY_RECOVERY=0` / `--no-general-fallback` to disable; see [nl_flow §6.1 / §10](docs/nl_flow.md)), and the user-memory profile injected when one exists (`RELAY_PROFILE=0` to disable).

```bash
uv run python scripts/run_plan.py --yes "帮我点三杯蜜雪冰城蜜桃四季春"
uv run python scripts/run_plan.py --yes "帮我找一台适合学生的平板电脑，预算2000以内"
uv run python scripts/run_plan.py --dry-run "把这段材料整理成一份中文总结文档"
```

## Run tests

```bash
uv sync --no-install-project --extra dev
uv run python -m unittest discover -s tests -v            # device-less; planner/runner unit tests, no adb needed
uv run python scripts/validate/validate_manifests.py      # manifest schema + prompt_template rules (CI gate)
```

Real-device runs (not unit tests) go through the entry points directly — see [Run it](#run-it-real-device-multi-vlm): `python -m agents.runtime.native_runner <pkg> "<goal>"` for a single app, `scripts/run_plan.py --yes` for the NL flow, `scripts/run_benchmark_test.py` for the A/B benchmark. They require a connected Android device with the target apps installed and `com.android.adbkeyboard/.AdbIME` available (the runner enables/restores the IME itself).

Copy `.env.example` to `.env` and fill in your values (LLM endpoint required). `test-results/` and `traj_logs/` are gitignored — do not commit trajectories containing user data.

## Documentation

| Document | Description |
| --- | --- |
| [NL cross-app flow](docs/nl_flow.md) | Synthesis, three-stage routing, validation, leg judge, failure recovery, coverage fallbacks, route solidification |
| [Cross-app planner](docs/cross_app_planner.md) | Planner pipeline, CLI usage, plan caching, real-device examples |
| [Manifest conventions](docs/manifest_conventions.md) | Language convention, `prompt_template`, `x_capture_full_reply`, card `swipe` direction, key capability fields |
| [Prompt templates](docs/prompt_template.md) | Templated submit prompts: slots, optional segments, load-time validation |
| [Capability taxonomy](docs/capability_taxonomy.md) | The controlled vocabulary behind capability ids |
| [Device backends](docs/device_backends.md) | The multi-platform backend layer (Android adb, iOS/HarmonyOS seams) |
| [Trajectory logging](docs/trajectory_logging.md) | Log directory layout, writers, rotation, consumers |
| [Device setup](docs/device_setup.md) | Real-device prep + per-benchmark app requirements |
| [Emulator testing](docs/emulator_testing.md) | AVD setup, install steps, remote observation |
| [Roadmap](docs/roadmap.md) | Productization phases P1–P5 with acceptance metrics (P1–P3 shipped) |
| [On-device app](android/README.en.md) | The Android app: architecture, host↔device seams, UI |

## MVP scope (v0.1)

Ten verified Android reference cards, **50 declared capabilities** total in the current catalog:

| App | Package | Capabilities | Card class |
| --- | --- | --- | --- |
| Amap (高德地图) | com.autonavi.minimap | POI search, navigation, ride hailing, trip planning | mixed |
| Tongyi Qwen (通义千问) | com.aliyun.tongyi | foundation_llm, train/ride/food/hotel/movie-event booking, product search/purchase guidance/order tracking | mixed |
| Ctrip (携程旅行) | ctrip.android.view | flights, hotels, trains, attractions, package tours | mixed |
| Gemini | com.google.android.apps.bard | foundation_llm, public-web retrieval, Google-service read/write tasks when authorized | mixed |
| Xiaohongshu (小红书) | com.xingin.xhs | community UGC Q&A via AI search | multi-node |
| WeChat (微信) | com.tencent.mm | Yuanbao chat surface, AI search | mixed |
| WPS Office | cn.wps.moffice_eng | AI doc / PPT / writing assist | single-bubble |
| Reddit | com.reddit.frontpage | Reddit Ask vertical community search and summarization | multi-node |
| Booking.com | com.booking | travel discovery, itinerary planning, accommodation search | mixed |
| Microsoft Copilot | com.microsoft.copilot | foundation_llm, nearby POI search, product search | single-bubble |

*Card class* (single-bubble TextView vs. multi-node RecyclerView) drives the reply-extraction strategy — see Tech Report §4 / §5.4.

Quality bar per card: all required SPEC fields populated, ≥2 real example prompts per capability, verified manually within 30 days of submission, `handoff_to_user_required` correct for every irreversible capability.

## Known blockers

- **Taobao server-side risk control ("访问被拒绝").** The Taobao shopping capabilities are now hosted in the 千问 (Qwen) card and routed through the Taobao backend — the standalone `com.taobao.taobao` card has been retired, since Taobao's in-app assistant *is* 千问 and the Qwen-hosted path goes through the same fulfillment backend without driving the Taobao app's GUI directly (the safer route). The risk-control wall may still surface on the deep-link target pages of `purchase_guidance` / `order_food` (the 淘宝闪购 local-delivery card): a server-rendered "亲，访问被拒绝" wall (or a one-time identity gate) instead of the product / local-delivery path. This is account- and device-level 风控, **not** an adapter or manifest bug: the entry path executes correctly and the failure happens on the deep-link target page *after* the in-app agent fires. Mitigations: sign the device into an account with normal purchase history, clear pending real-name / device-trust checks in 我的淘宝 → 设置 → 账号与安全, and avoid running the same risk-controlled capability back-to-back on a freshly-imaged device.

## What this project is *not*

- **Not a GUI agent.** We navigate to an in-app agent's input field, not the app's general UI. For general GUI agents, see MobiAgent / Mobile-Agent / AutoGLM.
- **Not a scraper.** Cards describe entry paths and capabilities, not data extraction. A conforming router does not read app data the user did not put there.
- **Not affiliated with any phone OEM or app vendor.** Neutral community spec. Vendors can publish official cards or not; the community can write one either way.
- **Not a challenger to A2A or MCP.** Forward-compatible by design (see SPEC §14). When apps ship A2A, cards become a thinner shim or disappear.

## Getting involved

- **Reading the design:** start with the [Tech Report](report/RelayAgent-TechReport.md), then [SPEC.md](SPEC.md) and [SPEC-OPEN-QUESTIONS.md](SPEC-OPEN-QUESTIONS.md).
- **Where this is headed:** the [productization roadmap](docs/roadmap.md) — five phases with acceptance metrics. **P1 (failure recovery), P2 (streaming capture), and P3 (user memory) have shipped**; P4 (card CI / semi-automatic authoring) and P5 (platforms / OEM integration) are open — a good place to pick up work.
- **Submitting a card:** see [CONTRIBUTING.md](CONTRIBUTING.md).
- **Code of conduct:** see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- **Discussion:** GitHub Issues.

## Acknowledgements

- [**MobiAgent**](https://github.com/IPADS-SAI/MobiAgent) (SJTU IPADS) — the pure-GUI mobile-agent line we position against, and the structural model for our [Tech Report](report/RelayAgent-TechReport.md).
- [**MobileWorld**](https://github.com/Tongyi-MAI/MobileWorld) (Tongyi MAI) — one of our three evaluation benchmarks, and the source of the `general_e2e` pure-GUI baseline used in the A/B comparison.

## License

Apache-2.0. See [LICENSE](LICENSE). Chosen for permissive enterprise use — the design only works if phone OEMs can adopt it without legal friction.
