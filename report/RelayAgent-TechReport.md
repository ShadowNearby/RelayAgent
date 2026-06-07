# RelayAgent: A Discovery Layer for Delegating User Requests to In-App AI Agents

> **Status:** Working draft (2026-06-02). §7–§8 are backed by a measured A/B
> benchmark (4 configurations, real device). The reported numbers are the
> **n=3 re-run of 2026-06-02**, executed after the per-step latency trims and
> the three MobileWorld fork robustness patches (`MW_WAIT_SECONDS`,
> `MW_ADB_TIMEOUT`, self-start-server PIPE-deadlock fix) landed on `main`; all
> 21 runs completed end-to-end with zero hangs. Frozen data:
> `report/benchmark-data-n3.md` (current n=3) and `report/benchmark-data-n1.md`
> (Round-1 n=1, four-config incl. the only manual-UI measurement).
> §2 product/version claims are now verified against primary sources (2026-06,
> footnoted); §8.2.1 reports full 28-capability coverage; §8.8 carries the
> annotated case-study trajectories and links the demo recordings. Modeled
> structurally on the MobiAgent
> technical report (arXiv:2509.00531) and GUI-agent report conventions
> (Mobile-Agent, AppAgent), but the chapter layout follows *our* contribution — a
> discovery layer + handoff contract, not a trained model or an automation engine.

---

## Abstract

OS-level assistants (HarmonyOS Xiaoyi, Apple Intelligence) and standalone GUI
agents both struggle to *act inside* third-party super-apps: the vendor-API route
(A2A, App Intents, AppFunctions, HMAF) needs each app to publish an endpoint that
most super-apps never ship, while the pure-GUI route re-drives the entire
interface — brittle, slow, and redundant with work the app could do itself. We
observe that most super-apps **already embed their own logged-in AI agent** (Amap's
assistant, Yuanbao in WeChat, Taobao/闪购's assistant, Xiaohongshu's 点点, WPS AI) —
an agent that already holds the user's login, address, and payment context and
already knows how to do the task. We argue for a **third path**: rather than
re-implementing the task (pure GUI agents) or waiting for a vendor endpoint
(A2A / App Intents), **delegate the user's intent to the app's own embedded agent**.
Realizing this paradigm needs not a smarter automation model but a *contract* that
makes delegation systematic and safe — a machine-readable, per-app **discovery
layer** telling an OS-level agent which apps embed an agent, where its input box
is, what it can do, and, critically, **when control must return to the user**. We
present **RelayAgent**, which operationalizes delegation-to-in-app-AI with three
pieces: a per-app card specification (*discovery*), a GUI-mediated relay adapter
that materializes a card into deterministic device actions (*access*), and a
`handoff_to_user_required` contract that returns control before any irreversible
action — payment, ride confirmation, order submission (*safety*). On a real-device
benchmark over two representative tasks (a food order and a cross-app
discover-then-ride flow), the paradigm pays off in two stages. **First**, merely
routing intent into the in-app agent (vs. hand-driving the native UI) is
*necessary but not sufficient*: it cuts cost by **−68%** where the task spans a
long cross-app navigation, yet yields essentially nothing (**+2.5%**, flat) on a
single-app order still driven step-by-step from screenshots. **Second**, *structured
delegation* (RelayAgent) plus two app-agnostic efficiency optimizations (a
two-stage reply-completion precheck and an accessibility-scrape-first text path)
reduces VLM token cost by
**2.4–3.6× versus an un-optimized delegation baseline** and by **11–19× versus a
pure step-by-step VLM agent driving the same in-app assistant** (and 19–34× versus
a pure-VLM agent driving the native UI by hand), while producing **far more
predictable** cost: RelayAgent's per-task token count is nearly identical across
repetitions (e.g. 3987 / 3986 / 3950 on the food-order task), whereas the pure-VLM
agent's cost varied several-fold run to run on the identical task (38k–97k tokens
at 46–379 s, all three runs reaching the same pre-payment screen), with still
larger swings — premature termination and runaway looping — seen in earlier
exploration.

**Keywords:** mobile agents, GUI agents, agent interoperability, in-app AI assistants, task delegation, handoff.

---

## 1. Introduction

Two trends are reshaping how users invoke functionality on a phone. First,
OS-level assistants increasingly try to act on the user's behalf across apps.
Second — and less remarked upon — individual super-apps now ship their *own*
in-app AI agents: Amap has an assistant tab that plans trips and hails rides,
WeChat embeds Yuanbao, Taobao and its 闪购 (instant-retail) channel carry a
shopping assistant, Xiaohongshu exposes community-knowledge search through 点点,
and WPS has an AI writer/reader. For a large fraction of real user intents, the
capability the user wants **already exists, already logged in, already holding the
user's address and payment context**, inside an app.

Yet an OS-level agent cannot easily reach those embedded agents. The two existing
bridges both fail in the open ecosystem:

- **Vendor-cooperation APIs** — Google's A2A, Apple App Intents / Shortcuts,
  Android AppFunctions, HarmonyOS HMAF. These are clean, typed, and reversible,
  but adoption is gated on the *app vendor* publishing an endpoint or function.
  The long tail, and most Chinese super-apps, do not.
- **Pure GUI agents** — Mobile-Agent, GUI-Owl, MobiAgent, AppAgent, A3, AutoGLM.
  These *perform the task themselves* by driving the full UI. They are brittle,
  slow, detectable, legally gray, and — most wastefully — they re-do work the
  app's own agent already does, re-navigating login, location, and payment state
  the in-app agent already holds.

**Thesis.** The missing piece is not a better automation model but a *discovery
layer*: a contract that tells an OS-level agent which apps embed an agent, where
its input lives, what it can do, and when control must return to the user.
Delegation, not re-implementation. RelayAgent is that layer — one card per app,
GUI-mediated by default, vendor-cooperation-*optional*, and forward-compatible
with A2A (when an app does publish an endpoint, the card becomes a thin shim).

A second, empirical motivation runs through this paper. Because RelayAgent
delegates to an agent that *already knows how to do the task*, it does far less
per-task reasoning than a GUI agent that re-derives every step from pixels. Our
benchmark (§8) quantifies this: across the same task and the same in-app
assistant, delegation plus two app-agnostic optimizations cut token cost by an
order of magnitude and — equally important for a system — made the cost
**predictable**, where the pure-VLM baseline's cost varied several-fold run to run
(and far more in earlier exploration).

### 1.1 Contributions

1. **The delegation paradigm** — route the user's intent into the app's *own
   logged-in in-app agent*, occupying the gap between re-driving the full UI (pure
   GUI agents) and waiting for a vendor endpoint (A2A / App Intents). To our
   knowledge RelayAgent is the first to treat a *third-party* app's **own embedded**
   assistant as a **GUI-mediated, endpoint-free delegation target** — distinct from
   driving the UI oneself (automation camp), from vendor-published endpoints
   (A2A / App Intents / AppFunctions), and from a vendor wiring its *own* assistant
   to its *own*-ecosystem services (e.g. Alibaba's Qwen app; §2.5). The leverage is
   the login / address / payment context the in-app agent already holds. (§1, §2)
2. **The card specification (SPEC v0.1)** — the machine-readable per-app contract
   that makes the paradigm *discoverable and reproducible*: launcher entry path,
   capability list, example prompts, latency hints, and the
   `handoff_to_user_required` policy, with a JSON-Schema mirror as the normative
   validator. (§4)
3. **The `handoff_to_user_required` contract** — confirm-before-irreversible-action
   is becoming an industry consensus (Gemini alerts before purchases; recent
   Agent-OS forecasts keep "the last step" with the user, inside a security
   boundary — §2.2). Our contribution is therefore not the safeguard itself but its
   *form*: encoding it **declaratively, per capability** in the card, so an OS agent
   knows *before* acting which capabilities must return control to the user, and
   where — the line between principled delegation and an unguarded chatbot call.
   (§4.1)
4. **A GUI-mediated relay adapter, with two app-agnostic efficiency optimizations**
   — materializes a card into deterministic device actions (accessibility-tree-first,
   provider-agnostic across VLMs), adds a two-stage (perceptual-hash + a11y-text-hash)
   reply-completion precheck and an a11y-scrape-first text path (measured A/B), and
   supports single-app natural-language routing. (§5–§7)
5. **A four-configuration efficiency study** on a real device, isolating (a) the
   paradigm's value (using the in-app agent at all), (b) structured delegation vs.
   re-driving the UI, and (c) the two optimizations — plus a *predictability*
   result. (§8)

---

## 2. Related Work

We position RelayAgent as **delegation**, against two prior camps (§2.1–§2.2), one
naming clarification (§2.3), and a fast-moving industry trend — first-party vendor
integration (§2.5). The comparison table (§2.4) is the spine.

> Product/version claims below were verified against vendor docs and primary
> sources as of 2026-06 (see footnotes); the conceptual placement is stable.

### 2.1 Vendor-cooperation interoperability (the API camp)

A2A, Apple App Intents / Shortcuts, Android AppFunctions, and HarmonyOS HMAF all
let an external agent invoke app functionality through a vendor-published
endpoint or function descriptor.

- **A2A (Agent2Agent).** Originated at Google and **donated to the Linux
  Foundation in June 2025** (now a vendor-neutral project with AWS, Cisco,
  Microsoft, Salesforce, SAP, ServiceNow among founding members). Its "Agent
  Card" is a JSON descriptor advertised over HTTP / JSON-RPC 2.0 for agent
  discovery.[^a2a]
- **Apple App Intents / Shortcuts.** Apple's framework for an app to expose
  actions to Siri, Shortcuts, Spotlight, and Apple Intelligence (via assistant
  schemas / App Intent domains).[^appintents]
- **Android AppFunctions.** A Jetpack mechanism (`androidx.appfunctions`) for an
  app to annotate functions callable by an on-device agent (e.g. Gemini), indexed
  through AppSearch. **Experimental / alpha as of 2026-06 — not yet GA**, and the
  Gemini integration remains in private preview.[^appfn]
- **HarmonyOS HMAF (Harmony Agent Framework).** Huawei's agent framework shipped
  with HarmonyOS 6 (October 2025), defining how apps / 元服务 expose capabilities
  to the system assistant 小艺 (Xiaoyi).[^hmaf]

Strengths: typed arguments, clean error semantics, reversible calls, OS-mediated
permissions. Weakness: the app vendor must do the publishing. For the open long
tail — and for most Chinese super-apps, which expose rich in-app assistants but no
third-party endpoint — this camp simply does not apply. RelayAgent treats vendor
cooperation as *optional*: when an endpoint exists, a card degenerates to a shim
over it (§10); when it does not, the GUI entry path still works.

[^a2a]: A2A Protocol site (a2a-protocol.org); Linux Foundation, "Launches the
Agent2Agent Protocol Project" (2025-06); Google Developers Blog, "Google Cloud
donates A2A to Linux Foundation."
[^appintents]: Apple Developer, "App Intents" / "Integrating actions with Siri
and Apple Intelligence."
[^appfn]: Android Developers, "AppFunctions" (developer.android.com/ai/appfunctions);
AndroidX `appfunctions` release notes (alpha).
[^hmaf]: Huawei HarmonyOS 6 launch coverage (2025-10); HMAF = Harmony Agent
Framework (鸿蒙智能体框架).

### 2.2 Pure GUI mobile agents (the automation camp)

Mobile-Agent (v1/v2/v3.x, Alibaba),[^ma] GUI-Owl (Alibaba, the Mobile-Agent-v3
base model),[^owl] MobiAgent (SJTU IPADS),[^mobi] AppAgent (Tencent),[^appagent]
AutoGLM (Zhipu),[^autoglm] and MobileUse[^mobileuse] perform tasks by driving the
full UI from screenshots, step by step. (We benchmark such agents on a shared
arena — A3, the *Android Agent Arena*[^a3], is an evaluation platform for this
class, not an agent itself.) RelayAgent reuses their transport techniques —
accessibility-tree grounding, VLM fallback localization — but *not* their premise.
Instead of re-performing the task, it **delegates** to the app's own logged-in
agent and returns. The novelty is therefore the *delegation paradigm* — made
discoverable by the card and safe by the handoff contract — not the automation.

[^ma]: X-PLUG/MobileAgent; Mobile-Agent v1 (arXiv:2401.16158), v2
(arXiv:2406.01014, NeurIPS 2024), v3 (arXiv:2508.15144).
[^owl]: GUI-Owl, released with Mobile-Agent-v3 (arXiv:2508.15144); Qwen2.5-VL-based.
[^mobi]: MobiAgent, arXiv:2509.00531 (IPADS-SAI/MobiAgent).
[^appagent]: AppAgent, arXiv:2312.13771 (TencentQQGYLab/AppAgent).
[^autoglm]: AutoGLM, arXiv:2411.00820 (Zhipu); Open-AutoGLM open-sourced 2025-12.
[^mobileuse]: MobileUse, arXiv:2507.16853 (MadeAgents/mobile-use), NeurIPS 2025.
[^a3]: A3: Android Agent Arena, arXiv:2501.01149 — a dynamic evaluation platform
(201 tasks over 20 live apps), used here as a benchmark, not a baseline agent.

Our evaluation makes the contrast concrete and quantitative: we run a pure
step-by-step VLM agent (MobileWorld's `general_e2e`) on the *same* tasks, both
with and without using the app's embedded assistant, and measure the cost gap
(§8). The pure-GUI agent is not only an order of magnitude more expensive but
**far less predictable** — on T1 its cost varied 2.5× in tokens (38k–97k) and 8×
in wall (46–379 s) across three runs, with larger swings (premature exit, runaway
looping) seen in earlier exploration, while RelayAgent reproduced its cost nearly
identically (3987 / 3986 / 3950 tokens, VLM-calls fixed at 2).

A recent long-form Agent-OS forecast[^agentos] corroborates both the camp taxonomy
above and the gap we target. It enumerates the routes by which a phone's system
agent is expected to reach app functionality — `AppFunctions` (local atomic
capabilities), `MCP / private tool`, `GUI Agent` (long-tail-app compatibility), and
`A2A` (cross-agent / cross-device) — i.e. exactly the API and automation camps plus
agent-to-agent. Notably, **delegating to an app's *own* embedded assistant is not
among them**: the path RelayAgent takes is absent even from a comprehensive 2026
forecast (an omission, not an explicit rejection — but it marks the route as
under-explored). The same forecast independently states the handoff principle we
encode — "the Agent can prepare, compare, fill forms, and explain, but the last
step must return control to the user," with "final signing and confirmation …
inside a security boundary" — confirming that the safeguard is now consensus and
that RelayAgent's contribution there is its *declarative, per-capability* encoding
(§4.1), not the idea.

[^agentos]: Gracker, 《万字长文推演：手机不再从 App 开始，Agent OS 如何接管任务入口》
("Long-form Forecast: Phones May No Longer Start from Apps — How Agent OS Takes Over
the Task Entry Point"), androidperformance.com, 2026-04-28. An industry forecast
(not a peer-reviewed system); cited for its taxonomy of capability-routing paths and
its statement of the handoff principle. Quotations are from the article's English
edition / our translation of the Chinese original.

### 2.3 Naming clarification: "Agent Card" (A2A) vs. RelayAgent cards

A2A's "Agent Card" is an HTTP-endpoint descriptor a vendor publishes for a
server-side agent. A RelayAgent card is a *GUI entry path* into an app's embedded
assistant — no endpoint, no server, zero vendor cooperation. The two share a word,
not a mechanism. (The project renamed away from the earlier `AppAgentCards` to
avoid colliding with the Tencent "AppAgent" line and Google's "Agent Card".)

### 2.4 Comparison

| Dimension | A2A / App Intents / AppFunctions / HMAF | Pure GUI agents (Mobile-Agent, MobiAgent, AppAgent …) | **RelayAgent (ours)** |
| --- | --- | --- | --- |
| Who does the task | The app's exposed API | The OS agent re-drives the full UI | The app's **own logged-in** in-app agent |
| Vendor cooperation | **Required** | None | **Optional** |
| Transport | HTTP endpoint | Full GUI automation | GUI-mediated **entry path** to the in-app agent |
| User context (login, address, payment) | Re-provided / token exchange | Re-navigated each run | **Already present** in the in-app agent |
| Irreversible-action safety | API-level | Ad-hoc | **`handoff_to_user_required` contract** |
| Brittleness surface | Low (typed) | High (whole UI) | Low (short entry path only) |
| Cost predictability | High | **Low (high run-to-run variance, §8)** | **High (nearly identical, §8)** |
| Novelty locus | — | Automation model | **Delegation-to-in-app-AI paradigm, made discoverable + safe** |

### 2.5 First-party vendor integration (the emerging third neighbor)

A third route is materializing fast in 2026, distinct from both camps above: a
vendor wiring its *own* assistant directly into its *own*-ecosystem services, so
the user transacts entirely inside one super-assistant. Alibaba's Qwen app is the
clearest case — its Jan 2026 launch absorbed Taobao Instant Commerce (闪购) and
Fliggy (飞猪; hotels, flights, and high-speed-train 高铁 tickets), a Feb 2026 upgrade
added Damai (大麦) ticketing, and in Mar 2026 it folded Amap **ride-hailing into the
assistant as a backend capability** — the Dec 2025 version still bounced the user out to the separate Amap
app to finish booking, and the Mar version removed that cross-app jump.[^qwenint]
This cuts both ways for us. It **validates the premise** — in-app assistants really
do carry login / address / payment and complete real transactions — and it **bounds
our niche**: such integration is *first-party and intra-ecosystem*. Qwen reaches
Alibaba-affiliated services; it does **not** reach a competitor's app or the long
tail — Meituan, WeChat / Yuanbao, Xiaohongshu's 点点, Ctrip, and WPS each keep their
*own* embedded assistant that Qwen cannot call, with no third-party endpoint to
call it through. RelayAgent targets exactly this **cross-vendor gap**: an OS-level
agent reaching *any* app's embedded assistant by GUI, without that app — or its
competitor — having wired or published anything. Where first-party integration
*does* exist, RelayAgent defers to it, the same forward-compatible stance it takes
toward published endpoints (§10).

[^qwenint]: Alibaba Group, "Qwen App Advances Agentic AI Strategy by Turning Core
Ecosystem Services into Executable AI Capabilities" (official, 2026-01-15);
Bloomberg, "Alibaba Takes Major Step to Link Taobao Shopping to Main AI App"
(2026-01-15); Caixin Global, "Alibaba's Qwen Launches AI Ride-Hailing Feature to
Rival Didi" (2026-03-24). The Damai / Freshippo / Tmall-Supermarket additions are
from Feb-2026 coverage (secondary). High-speed-train (高铁) ticketing via Fliggy is
included per author hands-on verification; note the official Jan-2026 scope
statement listed flights / hotels / attractions, so train ticketing is an
author-verified addition beyond that statement.

---

## 3. System Overview

```
NL request ─▶ Capability Router ─▶ Card (entry + capability + handoff)
                                       │
                                       ▼
                             Relay Adapter (MobileWorld)
                         cold-launch ▸ entry path ▸ type prompt
                         ▸ wait_for_reply ▸ a11y-scrape ▸ handoff
                                       │
                                       ▼
                         In-app AI agent does the work
```

A request enters as natural language. The **capability router**
(`agents/capability_router.py`) consults a catalog of cards and picks one
capability. The chosen card names a **launcher entry path** and a **capability**
with its slots, latency hint, and handoff policy. The **relay adapter**
(`agents/relay_agent.py`, built on a planner in `agents/action_planner.py`)
materializes that card into a deterministic action sequence: cold-launch the app,
open a fresh conversation, type the prompt, wait for the in-app agent's reply to
complete, scrape the reply text, and — when the capability is marked irreversible
— hand control back to the user. A shared adb helper (`agents/_adb.py`) backs
cold-launch, swipes, and device selection.

The remainder of the paper details the card spec (§4), the adapter mechanisms
(§5), natural-language routing (§6), the two efficiency optimizations (§7), and
the benchmark (§8).

---

## 4. The Card Specification

A card is a per-app YAML manifest (`manifests/*.yaml`) with a JSON-Schema mirror
(`spec/schema.json`) as the normative validator. It carries:

- **App identity & entry.** Package id plus the *launcher label* used to open the
  app. A practical gotcha the spec encodes: MobileWorld's `open_app` expects the
  desktop icon's display name (e.g. `千问`), not the package id
  (`com.aliyun.tongyi`); the adapter resolves entry from
  `embedded_agent.name` → `app_name` → package, in that order.
- **Capabilities.** Each has an id, a natural-language intent, slots, a
  `typical_latency_seconds` hint, a `handoff_to_user_required` flag, and
  `x_`-prefixed extensions for non-standard behavior
  (`x_prepare_fresh_conversation`, `x_capture_full_reply`, `x_max_wait_seconds`,
  etc.).
- **Card class.** Single-bubble (the whole reply renders in one TextView) vs.
  multi-node list (a RecyclerView of cards). This dichotomy drives extraction
  strategy (§5.4).

### 4.1 The handoff contract

The normative core of the spec is `handoff_to_user_required`. A capability marked
true must, in the adapter, **emit an `ask_user` and return control before any
irreversible call-to-action** — payment, ride confirmation, order submission. The
*principle* — let the agent prepare but keep the irreversible last step with the
user — is now broadly shared (Gemini alerts before purchases; Agent-OS forecasts
place "final signing and confirmation … inside a security boundary"; §2.2). What
the spec adds is making it **declarative and discoverable**: the flag travels with
each capability, so an OS agent can know *before* it acts which capabilities are
irreversible and stop at the right screen — rather than relying on a model
remembering, mid-task, not to tap "pay." This is what makes RelayAgent *delegation*
rather than *automation*: the in-app agent does the reversible preparation
(assemble the cart, set the destination, draft the order), and the human authorizes
the irreversible step. In the benchmark, every
handoff-required capability stopped at exactly the right screen — the food order at
the payment page (`立即支付 ¥28.4`) and the ride at the `立即打车` button — without
ever crossing it (§8, safety checks).

---

## 5. The Relay Adapter

The adapter is the execution engine; this is where the system-contribution rigor
lives. Source of truth: `agents/relay_agent.py` + `agents/action_planner.py`.

### 5.1 Cold launch & entry-path materialization

Every task begins with a cold launch (`agents/_adb.py:cold_launch()`:
`am force-stop` + monkey LAUNCHER + settle) so the first observation is a clean app
home, independent of prior state. The run scripts perform the launch and set
`RELAY_SKIP_OPEN_APP=1`; the planner then omits its own `open_app` step. (When
invoked without the scripts, the planner emits `open_app`, and the adapter's
`open_app` branch force-stops first, then lets MobileWorld tap the launcher icon
by **label, not package** — see §4.) Cold launch is not a detail: §8.4 notes that
a pure-VLM agent *without* a fresh-conversation step can misread stale on-screen
state and terminate prematurely; RelayAgent's explicit fresh-conversation step
(`x_prepare_fresh_conversation`) is what makes its behavior reproducible.

### 5.2 a11y-tree-first grounding (`tap_text`)

Any text/semantic selector first goes through a uiautomator XML dump, matching on
`text` / `content-desc` / `resource-id` and tapping the bounds center; VLM
grounding is only a fallback. On Chinese mobile UIs the a11y route is far more
reliable — VLM grounding once mislocated "新建对话" to (2, 965), whereas the dump
hits it almost every time. `tap_text` retries the dump up to 3× at 0.8 s spacing
to absorb drawer/animation latency, and all failure paths log at info/warning (an
earlier version buried them at debug, which read as "feature broken" when it was
really a dump error).

### 5.3 Reply-completion detection (`wait_for_reply`)

The adapter must decide when the in-app agent has finished generating. The budget
is **wall-clock seconds**, `max(5×typical_latency, 60)`, compared against
`time.monotonic()` — not a poll count, because each poll is a multi-second VLM
call and a poll-count budget under-reports real latency. The VLM returns
`{done, text}`; a `done=True` with `text==None` is treated as **untrusted** (if the
VLM cannot read any reply, generation almost certainly has not finished) and
polling continues. §7.1 makes each poll cheap.

### 5.4 Reply-text extraction

Reply text is scraped from the accessibility tree first
(`_extract_reply_text_from_dump`): cut below the user's bubble by y-coordinate,
filter chrome labels, and drop short "quick-reply chips" heuristically. The VLM is
asked only to judge `done`; its text (capped at ~500 chars) is *upgraded* to the
longer verbatim scrape whenever the scrape is longer. For multi-node-list cards,
`x_capture_full_reply` enables a scroll-capture loop that scrapes each frame and
stitches chunks. §7.2 details the savings.

### 5.5 Permission-popup auto-dismiss

Before each step, `_maybe_dismiss_permission_popup` checks the foreground package
(~130 ms) and fast-exits if it is not a known permission dialog; on a hit it dumps
the XML and taps the highest-priority *allow* label (`始终允许` > `允许` > `Allow`
…), never a deny, capped per task. This replaced a brittle "pre-authorize in
system settings" workaround.

### 5.6 Handoff emission

When the capability's `handoff_to_user_required` is set, the adapter emits an
`ask_user` carrying the scraped reply text, and blocks on stdin. A redirected /
EOF stdin (pipe or `< /dev/null`) terminates cleanly — that EOF **is success**, not
failure, and is how the benchmark's non-interactive runs end.

---

## 6. Natural-Language Routing

**NL routing** (`scripts/run_plan.py` + `agents/capability_matrix_router.py`) builds
a catalog of all app cards, synthesizes a flow, and resolves each app step
through the matrix-backed router, with a `--dry-run` mode for inspection. Each
app leg is then delegated to the native single-app runner with
`RELAY_FORCE_CAPABILITY` and `RELAY_INVOCATION_TEXT` set so the in-app invocation
stays pinned to that choice.

---

## 7. Efficiency Optimizations

Two app-agnostic mechanisms reduce the per-task VLM cost; neither needs per-card
tuning. Both are gated by environment flags (`RELAY_PRECHECK`, `RELAY_SCRAPE`,
default on) so the pre-optimization path is exactly reproducible for the A/B in
§8. This section explains *how* the §8 savings arise.

### 7.1 Two-stage precheck for reply-completion

Naively, `wait_for_reply` would issue a VLM `done`-judgment every tick. Instead:

- **Stage 1 (~25 ms/tick): perceptual screenshot hash.** Crop out the status bar
  (top 8%) and input area (bottom 18%), grayscale, downsample to 48×96, blake2b.
  If the hash differs from last tick, the screen is still mutating — the reply is
  streaming — so **skip both the dump and the VLM**. This covers the bulk of a
  reply's lifetime.
- **Stage 2 (~2.5 s/tick, only on a pixel-stable screen): a11y visible-text
  hash.** Dump the accessibility tree, hash the concatenated visible text, and
  diff against the previous tick. Text still growing ⇒ skip the VLM; two
  consecutive equal dumps ⇒ truly stable ⇒ call the VLM to confirm `done`.

Text-diff replaced an earlier "停止生成 / Stop generating" marker scan, which was
brittle (not every app has a stop button; some leave it visible after generation).
Two safeguards bound the worst case: a **circuit breaker** disables Stage 2 after
two consecutive dump failures (stable screens then go straight to VLM), and a
**watchdog** forces a VLM call after ≥5 consecutive precheck skips so an animated
UI element cannot stall detection. The effect is that done-detection VLM calls
collapse to the one or two ticks where the screen is actually stable — directly
visible in §8.4 as the gap between baseline and optimized reply-poll counts.

### 7.2 a11y-scrape-first reply-text extraction

The VLM is taken out of the *text-content* path entirely; it only judges `done`.
The reply text comes from the accessibility scrape (§5.4), which returns the full
visible text verbatim at no token cost. Both the happy path and the timeout path
upgrade the VLM's ≤500-char snippet to the longer scrape. This bypasses the VLM's
character cap: on a long single-bubble reply (a "ten loss functions" answer) the
VLM text was capped at ~120 chars while the scrape recovered ~1732 chars — **~14×
more content, zero extra VLM calls**, at the price of one ~2.5 s dump. For
`capture_full` multi-node lists, every scroll-frame extract is a scrape rather than
a VLM call, turning an N-VLM-call extraction into zero.

---

## 8. Evaluation

We isolate three questions with a four-configuration design on a real device:
(Q1) what does using the in-app agent at all save, vs. driving the native UI by
hand? (Q2) what does *delegating* to it (RelayAgent) save over a pure-VLM agent
driving that same assistant? (Q3) what do the two optimizations (§7) add? A fourth
result — cost *predictability* — emerges from the repetitions.

### 8.1 Experimental setup

- **Device / model.** Real Android device (`46180DLAQ004LW`) under MobileWorld;
  `qwen` VLM via the IPADS OpenAI-compatible gateway (provider-agnostic; Claude /
  Gemini are swap-in).
- **Tasks.** (T1) **order_food** — order three Mixue 蜜桃四季春 (default
  temperature/sugar). (T2) **flow** — find newly-opened, well-reviewed restaurants
  near Shanghai Jiao Tong University in Xiaohongshu, pick one, hail an Amap ride
  there (`xhs_to_amap_place`).
- **Configurations.**
  1. **MW manual-UI** — pure step-by-step VLM (`general_e2e`), instructed *not* to
     use any in-app assistant, navigating the native ordering / ride UI by hand.
  2. **MW general_e2e** — the same pure-VLM agent, but *using* the app's embedded
     assistant.
  3. **RA baseline** — RelayAgent with `RELAY_PRECHECK=0 RELAY_SCRAPE=0` (the
     pre-optimization path).
  4. **RA optimized** — RelayAgent with both optimizations on (default).
- **A controlled backend.** For T1, the manual UI is **Taobao 闪购**, and Taobao
  闪购's embedded assistant *is* Qwen (千问) — the same agent the RA / general_e2e
  configs drive. So T1's four configs place the **same order through the same
  backend**, varying only the interaction style. (Caveat: the manual leg is in the
  Taobao app and the assistant legs are in the Qwen app; same fulfillment channel,
  different host app.)
- **Protocol.** RA and general_e2e configs run **n=3** with `RELAY_TIMING=1`
  wall-clock capture; we report the **median** token count and wall-clock seconds.
  MW manual-UI was measured at n=1 (Round 1) and is not re-run. The reported
  figures are the **2026-06-02 n=3 re-run**, executed after the per-step latency
  trims and the three MobileWorld fork robustness patches landed (see §8.7); all
  21 runs completed end-to-end with zero hangs, and the conclusions reproduce the
  earlier 2026-06-01 n=3 round (only the exact token/wall medians shift within the
  run-to-run band). Token counts come from per-run `traj.json`
  (`llm_calls.usage_delta` for RA; aggregate `token_usage` for general_e2e/flow
  legs). Raw runs: `test-results/ab/n3/`; driver: `test-results/ab/run_n3.sh`;
  per-run aggregation: `scripts/aggregate_metrics.py`.

The deep token / wall-clock / predictability study (§8.2–8.4) uses the two
tasks above. Functional **coverage** across the full card catalog is reported
separately in §8.2.1: each capability in the then-current 7-card benchmark
catalog was driven
end-to-end at least once and reached its expected terminal state (a completed
reply for informational capabilities, the correct pre-CTA handoff screen for
irreversible ones). These are author-run functional passes (n=1 per capability),
not the instrumented n=3 cost runs.

Since that frozen benchmark round, the public manifest catalog has grown to
8 cards; Reddit Ask (`com.reddit.frontpage`) is the latest addition, an English
vertical community-search card whose entry path and `search_vertical_content`
capability were verified separately on Reddit 2026.22.0 / Android 16.

| App | Package | Capabilities | Card class |
| --- | --- | --- | --- |
| Amap | com.autonavi.minimap | POI search, navigation, ride hailing, trip planning | mixed |
| Tongyi Qwen | com.aliyun.tongyi | chat, train/ride/food/hotel/movie booking, product search/compare/purchase/order tracking | mixed |
| Ctrip | ctrip.android.view | flights, hotels, trains, attractions, packages | mixed |
| Gemini | com.google.android.apps.bard | foundation LLM, public-web retrieval, Google-service tasks | mixed |
| Xiaohongshu | com.xingin.xhs | community Q&A via AI search | multi-node |
| WeChat | com.tencent.mm | Yuanbao chat, AI search | mixed |
| WPS Office | cn.wps.moffice_eng | AI doc/PPT/writing | single-bubble |
| Reddit | com.reddit.frontpage | Reddit Ask vertical community search | multi-node |

#### 8.2.1 Capability coverage (historical benchmark catalog + added Reddit smoke pass)

The rows below are the historical benchmark-catalog coverage rows, plus the
new Reddit smoke pass. "Handoff" marks capabilities with
`handoff_to_user_required: true` — for these, success means the adapter emitted
the `ask_user` at the correct pre-CTA screen *without* crossing the irreversible
action; for the rest, success means the reply was captured and the task ended
cleanly.

| App | Capability | Handoff | Result |
| --- | --- | :---: | :---: |
| 高德地图 | find_nearby | — | ✓ |
| 高德地图 | navigate_to | ✓ | ✓ |
| 高德地图 | hail_ride | ✓ | ✓ |
| 高德地图 | plan_trip | — | ✓ |
| 通义千问 | chat | — | ✓ |
| 通义千问 | book_train | ✓ | ✓ |
| 通义千问 | order_food | ✓ | ✓ † |
| 通义千问 | hail_ride | ✓ | ✓ |
| 通义千问 | book_hotel | ✓ | ✓ |
| 通义千问 | book_movie | ✓ | ✓ |
| 通义千问 | search_product | — | ✓ |
| 通义千问 | compare_products | — | ✓ |
| 通义千问 | buy_product | ✓ | ✓ † |
| 通义千问 | track_order | — | ✓ |
| 携程旅行 | chat_travel_qa | — | ✓ |
| 携程旅行 | book_flight | ✓ | ✓ |
| 携程旅行 | book_hotel | ✓ | ✓ |
| 携程旅行 | book_train | ✓ | ✓ |
| 携程旅行 | plan_trip | ✓ | ✓ |
| 携程旅行 | search_attraction_info | — | ✓ |
| 小红书 | qa_community_knowledge | — | ✓ |
| 微信 | ai_search | — | ✓ |
| WPS Office | chat | — | ✓ |
| WPS Office | ai_ppt | ✓ | ✓ |
| WPS Office | quick_writing | ✓ | ✓ |
| WPS Office | doc_reading | ✓ | ✓ |
| WPS Office | web_summary | — | ✓ |
| Reddit | search_vertical_content | — | ✓ |

**Coverage: the frozen benchmark catalog reached its expected terminal states;
Reddit's added `search_vertical_content` smoke pass also reached reply capture.**
The historical handoff capabilities all stopped at the correct pre-CTA screen
(cf. §8.6).

> † **Taobao risk-control caveat.** The Taobao shopping capabilities are now
> hosted in the 千问 (Qwen) card and routed through the Taobao backend — the
> standalone Taobao card was retired because Taobao's in-app assistant *is* 千问,
> and the Qwen-hosted path reaches the same fulfillment backend without driving
> the Taobao app's GUI directly (the safer route). The Taobao backend still
> enforces strict server-side risk-control: the deep-link targets of `buy_product`
> / `order_food` can trip an account/device-level "访问被拒绝" wall or a
> one-time identity-verification gate. The entry path and handoff behavior are
> sound (both reached the pre-CTA screen on a clean account); we mark these ✓ for
> functional coverage while flagging the operational hazard; see §9.

### 8.2 Token cost (the headline result)

Median token cost per task (n=3 for RA / general_e2e; manual-UI n=1):

**T1 — order_food** (same Taobao-闪购 / Qwen backend, interaction style varies):

| Configuration | Median tokens | Token runs (n=3) | VLM calls | vs RA optimized |
| --- | ---: | --- | ---: | ---: |
| MW manual-UI (no assistant) | 75463 | (n=1) | — | 18.9× |
| MW general_e2e (uses assistant) | 77347* | 38282 / 77347 / 96888 | — | 19.4× |
| RA baseline | 9585 | 6788 / 9585 / 9594 | 3–4 (med 4) | 2.4× |
| **RA optimized** | **3986** | 3987 / 3986 / 3950 | **2** | **1×** |

\* general_e2e on T1 is **high-variance** (see §8.4); we report the full-set
median, **77347** (of 38282 / 77347 / 96888, at 5 / 9 / 11 steps). All three runs
*succeeded* — each reached the order-confirmation/payment screen with the cart
assembled (3 cups, default spec) and stopped before paying. The spread reflects
how many intermediate selection cards the assistant surfaced for the re-driving
agent to click through (plus per-step gateway latency), **not** success vs.
failure: the cheapest run (38282) is simply the one where the assistant
auto-assembled the cart in a single turn, leaving the agent nothing to click.

**T2 — flow** (xhs→amap, sum of discover + ride legs):

| Configuration | Median tokens | Token runs (n=3, total) | vs RA optimized |
| --- | ---: | --- | ---: |
| MW manual-UI (no assistant) | 294695 | (n=1) | 34.0× |
| MW general_e2e (uses assistant) | 95296 | 95296 / 95273 / 103988 | 11.0× |
| RA baseline | 31174 | 34247 / 22535 / 31174 | 3.6× |
| **RA optimized** | **8662** | 8662 / 5705 / 11233 | **1×** |

Reading the gradients answers Q1–Q3:

- **Q1 (use the in-app agent at all).** On T2 the assistant clearly pays off:
  294695 → 95296 (**−68%**). On T1 it does *not*: 75463 → 77347 (**+2.5%,
  essentially flat**). A pure-VLM agent driving the assistant re-derives every
  store-pick / add-to-cart step from full screenshots, so completing the order
  costs about the same as hand-driving the native UI. (The cheapest general_e2e
  run, 38282, is not an outlier failure — it also reached the payment screen; it
  was just the run where the assistant auto-assembled the cart in one turn. See the
  table footnote and §8.4.) This is the key asymmetry: **the payoff of *using the
  in-app assistant at all* scales with task complexity** — negligible on a short
  order whose native path is already shallow (T1: +2.5%, ~9 steps either way),
  but large on a discovery-heavy task where the assistant collapses a long
  navigation into one conversational turn (T2 discover: **23 native-UI steps by
  hand → 7 with the assistant**, −68% tokens). Crucially this is the payoff of the
  *assistant*, not of RelayAgent, and a re-driving agent (general_e2e) only banks
  it on the heavy task. **RelayAgent's structured delegation, by contrast, wins on
  *both* (T1 19.4×, T2 11.0×, Q2)** — because what it removes, per-step VLM
  re-driving, is paid on every task regardless of complexity. So on T1, merely
  *using* the assistant buys almost nothing; only the structure does.
- **Q2 (delegate vs. re-drive).** general_e2e → RA optimized: **−94.8% / 19.4×** on
  T1 and **−90.9% / 11.0×** on T2. The gap is the cost of re-deriving every step
  from full screenshots (general_e2e also re-sends a 3-image visual history each
  step) versus RelayAgent's structured plan with zero-token a11y taps and a
  discovery manifest that pins the assistant's entry path. This gap is **not a
  screenshot artifact**: a token-efficient *a11y-text* re-driving baseline (no
  screenshots, §8.9 item 2) still costs **11.9×** RA optimized (median 47302 tok),
  because re-driving pays one LLM round-trip per UI step (≈50) regardless of
  per-call input size, whereas RA issues 2.
- **Q3 (the two optimizations).** RA baseline → RA optimized: **−58.4% / 2.4×** on
  T1 and **−72.2% / 3.6×** on T2, from precheck (fewer done-detection VLM polls:
  T1 VLM 4→2) and scrape (zero-token text extraction).

**Token composition — the gap is screenshots, and the dollar gap is smaller.**
Across every config the count is **~97–99% *prompt* tokens**, not completion
(manual-UI 98.9%, general_e2e ~98.8%, RA baseline 97.5–98.1%, RA optimized
96.2–97.1%): the cost is dominated by the screenshots fed to the VLM, not by what
it writes back. Per-call instrumentation makes the mechanism concrete — one
screenshot costs **≈2783 prompt tokens** on this gateway, and RA optimized issues
**exactly one** image (the single done-confirming poll; its 3987 = a 1054-token
text-only router call + one 2783-token image), RA baseline two-to-three (one per
extra done-poll), whereas general_e2e and manual-UI send a screenshot **per step
plus a 3-frame history** — on the order of **~30 screenshots** over a 9-step run.
The headline 19× is therefore, concretely, **~1 image vs ~30**. Because
prompt/image tokens are priced well below completion tokens and are cacheable, the
**dollar** gap is smaller than this token gap; we report the token / call-count
axis and restate it in dollars next.

**Dollar restatement.** Priced at OpenRouter's public rate for the served model
`qwen3.5-27b` ($0.195/M input, $1.56/M output, accessed 2026-06-03), per-task cost
is RA optimized **$0.00098** (~0.1¢), RA baseline $0.0021 (2.2×), general_e2e
$0.0163 (16.6×), manual-UI $0.0158 (16.1×). The dollar multiplier (16.6×) is
modestly *smaller* than the token multiplier (19.4×): at a 1:8 input:output price
ratio, RA optimized's slightly higher completion fraction (3.8% vs general_e2e's
1.2%) lifts its effective per-token price just enough to compress the gap ~14%.
That compression is ratio-determined, so it is unaffected by the promotional
discount baked into the listed price — only the absolute cents scale. Prompt
caching, which the ~97–99% prompt-heavy high-volume configs are most amenable to,
would widen the absolute gap further. Full method + a T2 cross-check (same
pattern: $9.4× vs 11.0× token): `report/cost-dollar-analysis.md`.

**What the 19× is — and isn't — attributable to.** The lever is *delegation*, not
the manifest. The expensive, run-to-run-variable cognition — which stores exist,
what goes in the cart, which route — is performed by the in-app assistant and
**never enters RelayAgent's plan**; that is why RA's plan is a short, *fixed*
sequence (cold-launch → open conversation → type → wait → handoff) that the card
can capture once. The manifest is therefore an **optimization downstream of
delegation**, not an independent prior that does the task. Even the one
task-touching part of the order_food card — its post-result taps — is the fixed
accept-defaults pattern `选这个` → `选好了` (tap whatever store/spec the assistant
recommended, then stop before `支付宝付款`), not a decision about which store or
item; genuine choices are deferred to the user via `ask_user`. A re-driving agent
(general_e2e) is expensive precisely because the task it performs is *not*
fixed-step and cannot be scripted — delegation is what makes the relay scriptable
in the first place. A manifest-free delegation relay (`RELAY_NO_MANIFEST=1`, §8.9
item 1) measures the manifest's marginal contribution directly: it lands between
general_e2e and RA (≈14k tokens), splitting the 19× into **≈5.5× from the
delegation skeleton and ≈3.5× from the manifest's zero-token taps** — i.e. mostly
delegation, with the manifest a real but secondary optimization (that also buys
reliability — see §8.9 item 1).

### 8.3 Wall-clock — only at the re-drive→delegate gradient

Wall-clock is reported **only where it reflects an agent-design difference**: the
gradient from hand-driving the native UI, to re-driving it with a per-step VLM, to
delegating to the in-app assistant. RelayAgent is represented here by the
**baseline** config — the optimized config's *agent-controlled* wall is identical
(the precheck/scrape optimizations change VLM-call count, not the work the agent
does; justified below), so baseline↔optimized is **not** a wall-clock comparison we
make.

| Config | T1 wall_s (median) | T2 wall_s (median) |
| --- | ---: | ---: |
| MW manual-UI (no assistant) | 193 | 717 |
| MW general_e2e (uses assistant) | 111| 166 |
| RA (RelayAgent) | 47.6 | 115.8 |

Here the savings are **real and large**, because each step *does less work*:
hand-scrolling notes (T2 discover: 23 native-UI steps) collapses to one assistant
turn; re-deriving every action from full screenshots + a 3-image history collapses
to a structured plan with zero-token a11y taps. A pure-VLM agent re-driving the UI
is materially slower (T2 166 vs 116 s), and its slowest runs far slower still (T1
379 s — an 11-step completion dragged out by per-step gateway latency; manual-UI
T2 717 s).

**Why we exclude RA baseline ↔ RA optimized from the wall-clock comparison.** That
delta is dominated by per-call LLM-gateway latency `V` (1.4–32 s on the shared lab
endpoint), a property of the **serving stack**, not of the agent design. A
same-session interleaved re-test makes this concrete: the optimized config's *total
VLM time* tracks its wall one-for-one across reps — T1 **4.8 / 7.9 / 24.6 s** of VLM
↔ **48.5 / 51.7 / 68.9 s** wall — i.e. the only thing moving the wall is the single
done-confirming VLM poll's gateway draw; the agent-controlled portion is identical
to baseline. The §7 optimizations (precheck, scrape) are therefore a **token /
call-count** win, evaluated on that agent-design-invariant axis in §8.2 (T1 VLM
4→2, −58%). Claiming a wall-clock delta on this axis would just be reporting gateway
noise — and this matches the convention in GUI-agent evaluation, where steps /
tokens / success-rate are the reproducible efficiency metrics while wall-clock, a
serving-stack artifact, is not a headline claim.

> Note: the per-step **sleep trims** (perf-trim: `step_wait_time 1.0→0.2`,
> `MW_WAIT_SECONDS 1.0→0.2`, poll-skip `0.8→0.3`) *are* a wall-clock win, but that
> is a before/after comparison of the **same config's fixed per-tick overhead**
> (order_food cold-launch→handoff ~70 s → ~51 s; poll tick 4.0 s → 1.8 s) — it
> trims the agent-controlled portion, independent of the gateway-bound VLM time
> discussed above. It is not a baseline-vs-optimized claim.

### 8.4 Predictability — RelayAgent is deterministic-ish; the pure-VLM agent is not

The most striking n=3 finding is variance. RA token counts are nearly identical
across repetitions — T1 optimized **3987 / 3986 / 3950** (VLM fixed at 2),
baseline **6788 / 9585 / 9594** (VLM 3/4/4, each extra done-poll ≈ +3k). The
pure-VLM `general_e2e` on the *same* T1 produced **38282 / 77347 / 96888 tokens at
46 / 111 / 379 s** — a 2.5× token / 8× wall spread. All three runs *succeeded*:
each reached the order-confirmation/payment screen with the cart assembled and
stopped before paying. The spread is **among successes**, set by how many
intermediate selection cards the assistant surfaced for the re-driving agent to
click through (5 / 9 / 11 steps) plus per-step gateway latency — not success vs.
failure. The re-driving design also admits two sharper failure modes, observed in
earlier (n=1) exploration outside this trio:

- **Premature termination:** after cold launch the Qwen app restores a prior
  conversation/order card; lacking a fresh-conversation step, the agent can read
  it as done and exit in 1–2 steps.
- **Runaway:** the agent never recognizes completion and loops toward the step
  cap, re-sending the full screenshot + 3-image history each step (one such
  exploration run reached a 50-step / 531,597-token tail).

The lesson is a system one: RelayAgent's explicit fresh-conversation step
(`x_prepare_fresh_conversation`) and structured plan make its cost *predictable*,
which a pure-VLM screenshot-to-action loop is not. For an operator paying per
token, a predictable ~4 k beats a 38 k–97 k spread (and a ~530 k tail in the worst
observed run). **Predictable cost is itself a system contribution.**

### 8.5 Reply-content quality recovery

Independent of count, the scrape path (§7.2) recovers reply *content* the VLM
truncates. On a long single-bubble reply the VLM text capped at ~120 chars while
the a11y scrape returned ~1732 chars — **~14× content recovery**, zero extra VLM
calls, one ~2.5 s dump. (T1's order-confirmation reply is short, so the order_food
chains show no content-recovery gap, as expected for a short-CTA capability.)

### 8.6 Safety checks

Every `handoff_to_user_required` run stopped before the irreversible CTA. Across
the three general_e2e ride repetitions, all stopped at the `立即打车` screen with
**zero** confirm taps; the Qwen food orders stopped at the payment handoff (the
Round-1 manual-UI food order stopped at `立即支付 ¥28.4`). No order or ride was
ever placed.

### 8.7 Robustness (this round)

All 21 runs of the 2026-06-02 n=3 batch used the **self-start server path** (no
`--aw_host`) and completed without freezing. The earlier intermittent mid-run hang
(often surfacing right after `input_text`) was root-caused to MobileWorld's
`_start_server_background` streaming the server's stdout/stderr to an **undrained
`subprocess.PIPE`**: once a run's logs filled the ~64 KB kernel pipe buffer, the
server's logging thread blocked on `anon_pipe_write` and stopped answering
requests, hanging every client (confirmed via kernel `wchan`). The fork patch
streams server output to a logfile instead; a secondary `MW_ADB_TIMEOUT` caps any
stalled adb call. Together these made the long unattended n=3 batch reproducible.

### 8.8 Case studies (annotated trajectories)

One end-to-end trajectory makes the cost numbers concrete. Demo recording:
`assets/RelayAgentDemoOrder/RelayAgentDemoOrder.gif` (T1). Step strings below are
verbatim from the run `prediction` fields in `traj.json`.

#### 8.8.1 T1 order_food — RA optimized (run `n3_retest/order_food_optimized_r1`, 3987 tokens, 2 VLM calls)

The 11-step plan expanded to 16 runner steps (the single `wait_for_reply` step
held for 6 of them — see §5.3's non-advancing semantics). Only **two** LLM calls
were made in the entire task: one text-only `capability_router` pick (1054→75
tok, 1.4 s) and one `reply_watch` done-judgment (2783→75 tok, 3.4 s) — total VLM
time 4.8 s, ≈ all the wall-clock that isn't fixed-overhead. Reply text was scraped
from the a11y tree at zero token cost.

| Plan step | Action | Note |
| --- | --- | --- |
| 1/11 | tap_fraction | fresh conversation |
| 2/11 | tap_text | fresh conversation (uiautomator hit) |
| 3/11 | wait_text | `'发消息或按住说话…'` present (2471 ms) |
| 4/11 | tap_text | focus input (uiautomator) |
| 5/11 | input_text | `帮我点三杯蜜雪冰城蜜桃四季春，温度和糖度都用默认` |
| 6/11 | tap_bounds | submit |
| 7/11 | wait_for_reply | **precheck skip ×5** (screen changing, 0.0→7.2 s), then **1 VLM poll → done** @ 13.0 s |
| 8/11 | tap_text | select store (uiautomator) |
| 9/11 | tap_text | add to cart (uiautomator) |
| 10/11 | wait_ms | settle |
| 11/11 | **handoff** | `ask_user` carrying the scraped reply, stops at the order/payment screen |

The captured reply handed to the user: *"已为你找到附近多家蜜雪冰城门店的蜜桃四季春，
请选择你想要下单的店铺… 选好后我帮你加购3杯默认规格的蜜桃四季春。"*. This is the
canonical low-cost path: the two-stage precheck (§7.1) collapsed the streaming
window into a single done-poll, and the a11y scrape (§7.2) kept text extraction
at zero VLM calls — hence the near-constant 3987 / 3986 / 3950 token figure
(§8.4).

For contrast, `general_e2e` on the identical task and backend spent
**38282–96888 tokens over 5–11 steps** in the same n=3 round — all three runs
reached the same payment screen, but re-deriving each step (cart, store pick,
quantity) from full screenshots plus a 3-image visual history cost an order of
magnitude more than RA, and the step count (hence cost) swung run to run with how
much the assistant auto-resolved.

### 8.9 Threats to validity

We surface the evaluation's main soft spots and the experiments that would close
them, so the claims above are read at their actual strength.

1. **Manifest-isolation ablation — now run (`RELAY_NO_MANIFEST=1`).** To separate
   *delegation* from the *authored manifest* in Q2's 19×, we built a manifest-free
   delegation relay: it loads **no card** and drives the same delegation skeleton
   (fresh conversation → type the whole request → wait → accept-defaults advance →
   hand off before the irreversible CTA) with every affordance VLM-grounded at
   runtime; the only app fact it uses is the package id, exactly as general_e2e
   does. On T1 (n=3, `test-results/ab/nm/`) it lands where the framing predicts —
   **RA optimized 3987 < no-manifest ≈14147 < general_e2e 77347** — which
   decomposes the gap: general_e2e→no-manifest is **≈5.5×** (the delegation
   skeleton replacing free-form per-step re-driving — ~30 screenshots collapse to 5
   VLM calls), and no-manifest→RA is **≈3.5×** (the manifest's zero-token
   uiautomator taps replacing ~4 runtime VLM groundings). So the 19× is **mostly
   delegation**, with the manifest a real but secondary optimization — confirming
   §8.2. **Nuance:** reliability was **1/3** — only one run cleanly reached the
   payment handoff; one engaged the order then stopped early (safe but incomplete),
   and one mis-grounded the input box so the query never sent. So the manifest also
   buys *robustness*, not just tokens: runtime VLM grounding of affordances is flaky
   on this CN UI (the reason the card encodes them as selectors/bounds), echoing the
   `HISTORY_N_IMAGES` finding (item 2). n=3 is small and the token figure is from
   the successful path.
2. **Baseline token-efficiency, and token ≠ dollars.** general_e2e re-sends a full
   screenshot plus a 3-image visual history each step, so its count is dominated by
   image-*prompt* tokens. We now report the prompt/completion split (§8.2): every
   config is ~97–99% prompt tokens, one screenshot ≈2783 prompt tokens, and the
   19× is concretely ~1 image (RA) vs ~30 (general_e2e). Because prompt/image
   tokens are priced well below completion tokens and are cacheable, the **dollar**
   gap is smaller than the token gap; a more frugal pure-VLM input *modality*
   (a11y-text rather than raw screenshots) narrows Q2 only modestly — measured at
   11.9× RA below, not single digits. We therefore
   frame Q2 as a *token / call-count* result; a dollar restatement (§8.2,
   `report/cost-dollar-analysis.md`) shows the gap is 16.6× in dollars vs 19.4× in
   tokens — modestly compressed but the same order. We also ran the frugal-input
   variant (`HISTORY_N_IMAGES=1`, one screenshot/step instead of the 3-frame
   history; `test-results/ab/n3_hist1/`, n=3). It confirms the history is ~53% of
   general_e2e's per-step prompt load (~2.2k tokens/frame) — but a leaner baseline
   is *not* cheaper or fairer: success fell to **1/3** (the dropped frames carry
   task-critical prior state, so the agent loops on the quantity selector), and the
   two failures ran to the 50-step cap at **~332k tokens, 4× the 3-image runs**;
   even the one success (15,789 tokens) is still ~4× RA optimized. So the 3-image
   config is general_e2e's load-bearing working setup, not inflated padding, and
   RA's gap is not an artifact of an over-heavy baseline. We then tested the input
   *modality* directly with an **a11y-text baseline** (`agents/a11y_agent.py`,
   `test-results/ab/a11y/`, n=3): a pure re-driving agent fed the accessibility
   tree as text instead of screenshots, everything else held constant (uses the
   assistant, free-form per-step decisions, same task/model/gateway, and a
   fresh-conversation start matching general_e2e's input-box state). It came in at
   **37422 / 47302 / 49008 tokens (median 47302)** — only **1.6× cheaper than
   screenshot-fed general_e2e (77347)** and still **11.9× RA optimized**. The
   mechanism confirms the thesis: the a11y modality *did* cut per-call input ~3.5×
   (~770–970 prompt tokens/call vs. one screenshot's ~2783), but re-driving's call
   *count* did not shrink — it grew, to **48–50 LLM calls** (vs. RA's 2), because
   text-only navigation on this CN UI is harder (two of three runs looped to the
   50-step cap; reliability **1/3**, matching the no-manifest and
   `HISTORY_N_IMAGES=1` ablations). So Q2's gap is **not** a screenshot-modality
   artifact: a leaner-per-call input still leaves an ~12× gap, because delegation's
   durable lever is collapsing the task to ~2 round-trips, not shrinking each one.
   (Even granting a hypothetical a11y run at general_e2e's ~9-step count,
   9 × ~900 ≈ 8k tokens is still ~2× RA optimized.) set-of-marks input remains the
   one untested modality.
3. **Sample size and task breadth.** The instrumented cost/variance study is two
   tasks at n=3 (RA/general_e2e), manual-UI at n=1; the 28-capability table
   (§8.2.1) is n=1 author-run functional passes, not repeated success-rate
   measurement. We make no strong statistical claim from n=3 and report ranges, not
   confidence intervals.
4. **Predictability evidence provenance.** Within the reproducible n=3 trio the
   general_e2e spread is 2.5× tokens / 8× wall, all successes; the sharper
   premature-exit and runaway modes (§8.4) come from earlier *untracked*
   exploration and are not reproduced here. The claim is strongest as
   *RA-is-near-constant* (1.01×) and weaker as *general_e2e-is-wildly-unstable*
   until those modes are reproduced under instrumentation.
5. **Handoff safety is happy-path only.** §8.6 shows the handoff held across a
   handful of non-adversarial runs; we have not stress-tested the boundary — an
   assistant that auto-submits in one turn, a mis-annotated
   `handoff_to_user_required`, or a CTA whose label differs from the manifest's
   `stop_before`. The contract's robustness is asserted, not yet adversarially
   evaluated.
6. **Cross-round / cross-host comparison.** The manual-UI column is Round-1 n=1
   (pre-perf-trim) against a later n=3 RA round, and T1's manual leg runs in the
   Taobao host app while the assistant legs run in Qwen (shared fulfillment
   backend). The 18.9× / 34× manual-UI multipliers are therefore *indicative*; the
   strictly controlled, same-round comparisons are 19.4× / 11.0×.
7. **a11y hit-rate underpins the token story.** Zero-token taps assume uiautomator
   resolves nearly every selector; WebView-rendered content defeats it (§9). We do
   not yet report a catalog-wide a11y hit-rate / VLM-fallback rate; where a11y
   misses, grounding cost reappears.

---

## 9. Limitations & Known Blockers

Evaluation-internal threats (baseline cost, sample size, predictability
provenance, handoff safety, manifest-isolation ablation) are itemized in §8.9;
this section covers the broader system-level limits.

- **Sample size & scope.** The benchmark covers two tasks at n=3 (RA /
  general_e2e); MW manual-UI is n=1. Full 6-card / per-capability success rates
  (§8.2) are not yet measured. T1's manual leg uses a different host app (Taobao)
  than the assistant legs (Qwen), though the fulfillment backend is shared. (See
  §8.9 for the planned experiments that close these.)
- **Taobao server-side risk-control wall.** The Taobao shopping capabilities are
  now hosted in the Qwen card and routed through the Taobao backend (the standalone
  Taobao card was retired, since Taobao's in-app assistant *is* 千问 and the
  Qwen-hosted path reaches the same fulfillment backend without driving the Taobao
  app's GUI directly — the safer route). The Taobao backend's `buy_product` /
  `order_food` deep-link targets can still hit an account/device-level "访问被拒绝"
  wall (and a one-time identity-verification gate we hit mid-benchmark); this is not
  an adapter bug — the entry path is sound.
- **VLM grounding fallback** accuracy degrades on CN UIs when the a11y tree is
  empty (WebView-rendered content); Qwen-VL's normalized-vs-pixel coordinate
  ambiguity is handled by a heuristic in `_ground_text`.
- **Card maintenance.** Entry paths can drift as apps redesign their UI.
- **First-party integration may obsolete single-ecosystem cases.** Vendors are
  actively wiring their own assistants into their own services (Alibaba's Qwen;
  §2.5), which removes the need for an external relay *within* that ecosystem.
  RelayAgent's durable value is the **cross-vendor / long-tail** case — reaching
  apps whose vendor has neither folded them into a super-assistant nor published an
  endpoint; for already-integrated paths it defers to the first-party route.
- **Scope.** GUI-mediated relay only — no endpoint, no data scraping beyond the
  on-screen reply.

---

## 10. Conclusion & Future Work

RelayAgent's contribution is a *discovery layer + handoff contract* for delegating
to apps' own logged-in AI agents, not another automation model. The benchmark
shows the practical payoff of delegation: an order-of-magnitude token reduction
versus a pure-VLM agent on the same task and the same in-app assistant, a further
2.4–3.6× from two app-agnostic optimizations, and — the result we did not anticipate —
**predictable** per-task cost where the pure-VLM baseline varied several-fold run
to run (and far more in earlier exploration). Two regimes matter here. The payoff
of *using an in-app assistant at all* scales with task complexity — it is
negligible on a short task whose native path is already shallow but large on a
discovery-heavy one (T2: 23 native-UI steps collapse to 7). RelayAgent's
*structured* delegation, however, pays off on **both** simple and complex tasks
(T1 19.4×, T2 11.0×), because it removes the per-step VLM re-driving cost that a
pure-VLM agent incurs regardless of task complexity. We are deliberate about what
the optimizations buy: token/cost, not wall-clock (which the in-app assistant's
own latency dominates).

Future work: more cards (and OEM-published cards); richer handoff semantics;
non-Android platforms; and **A2A forward-compatibility** —
when apps ship endpoints, cards become a thin shim or disappear (SPEC §14).

---

## Appendix A. Reproducibility

- **Entry points.** `python -m agents.native_runner <pkg> "<goal>"` (single app) and
  `scripts/run_plan.py "<goal>"` (routed flow). Both cold-launch the target and set
  `RELAY_SKIP_OPEN_APP=1`.
- **A/B flags.** `RELAY_PRECHECK=0 RELAY_SCRAPE=0` reproduces the pre-optimization
  baseline; `RELAY_TIMING=1` writes a per-run `wall_clock.json`.
- **Pure-VLM baselines.** `mw test "<instr>" --agent-type general_e2e` (uses the
  in-app assistant) or with a "do not use any AI assistant" instruction
  (manual-UI). Cold-launch the app first.
- **Aggregation.** `scripts/aggregate_metrics.py <run-dirs>` (token / VLM-purpose /
  wall_s, with baseline→optimized delta). Driver `test-results/ab/run_n3.sh`
  (3 configs × 2 tasks × 3 reps → `test-results/ab/n3/`). Raw runs in
  `test-results/ab/` (gitignored); frozen data in `report/benchmark-data-n3.md`
  (current n=3) and `report/benchmark-data-n1.md` (Round-1 n=1, incl. manual-UI).
- **Environment.** MobileWorld real-device (adb + AdbKeyboard), Python 3.12, LLM
  config in `.env`. Registry-selection fix for file-based agents: commit
  `fe1682c` (branch `ab-benchmark-and-registry-fix`).

## Appendix B. Card example

A trimmed-but-faithful excerpt of `manifests/com.autonavi.minimap.yaml`, showing
the three normative pieces: **app identity & launcher entry**, the **entry path**
into the embedded assistant, and a **`handoff_to_user_required` capability**
(`hail_ride`). Comments are from the live manifest.

```yaml
spec_version: "0.1"
app_id:   "com.autonavi.minimap"
app_name: "高德地图"                 # launcher label, NOT the package id (§4)

embedded_agent:
  name: "高德 AI 助手"
  type: native_in_app_agent

  # ── Entry path: how to reach the in-app assistant's input box ──
  entry:
    primary:
      method: tap_sequence
      steps:
        - tap:  { text: "长按说话" }      # bottom-center AI tab
        - wait: { ms: 300 }
        # The AI tab opens in voice mode on first install; flip to text mode
        # only if the text-mode hint is absent. Stateful + non-idempotent, so
        # use a one-shot conditional tap rather than an unconditional one.
        - tap_unless_present:
            probe:  { text: "有什么问题尽管问我" }
            target: { screen_fraction: { x_ratio: 0.9051, y_ratio: 0.8797 } }
        - wait: { ms: 300 }
    fallback: []

  # ── Where the prompt goes / how it is sent ──
  invocation:
    input:
      field:                            # EditText has no id/content-desc;
        screen_fraction: { x_ratio: 0.4597, y_ratio: 0.8797 }
        text: "有什么问题尽管问我"        # hint text works only pre-focus
      max_chars: 500
    submit:
      trigger:                          # same ViewGroup: mic when empty, send-arrow when typed
        screen_fraction: { x_ratio: 0.9051, y_ratio: 0.8690 }
    prompt_template: "{{user_prompt}}"

  capabilities:
    - id: hail_ride
      description: >
        Order a ride via Amap's aggregator. Multi-step: disambiguate drop-off →
        choose car type → confirm estimate → pay.
      example_prompts:
        - "叫一辆经济型车去虹桥机场"
      executable: true
      side_effects: [payment]
      requires_login: true
      reversible: false
      handoff_to_user_required: true    # ← NORMATIVE: stop before the pay CTA
      typical_latency_seconds: 10
      # First VLM "done" reading is often the transient "找到N篇资料" spinner,
      # not the rendered ride card — keep polling/scrolling past it.
      x_capture_full_reply: { max_scrolls: 3 }
      failure_modes:
        - "Multi-terminal airports → drop-off disambiguation panel"
        - "Not logged in → redirects to login page"

provenance:
  last_verified: "2026-05-11"
  verified_app_version: "16.16.0.2001"
  verification_method: scripted
```

Reading it as a contract: the adapter cold-launches `com.autonavi.minimap`,
walks the `entry.primary` tap sequence to the assistant (handling the
voice/text-mode toggle), types `{{user_prompt}}` into the bounds-anchored input,
waits for the reply (capturing up to 3 scrolls of the ride card), and — because
`hail_ride` is `reversible: false` / `handoff_to_user_required: true` — emits an
`ask_user` at the `立即打车` screen, returning control to the human before the
single irreversible payment tap.

---

### Open measurement tasks (tracking — mirrors project TODO #1)

- [x] Freeze the baseline config as a runnable flag (`RELAY_PRECHECK`/`RELAY_SCRAPE`).
- [x] Instrument wall-clock + token logging per run (`RELAY_TIMING`, `aggregate_metrics.py`).
- [x] Run the four configs on two tasks, n=3 (RA/general_e2e). → §8.
- [x] Fill §8.2 (token), §8.3 (time — re-drive→delegate gradient only; baseline↔opt excluded), §8.4 (predictability), §8.5 (quality).
- [x] Re-run n=3 post latency-trim + fork robustness patches (2026-06-02); refresh §8.2–8.6, add §8.7 robustness.
- [x] §8.2 full historical benchmark-catalog / per-capability success-rate table. → §8.2.1; Reddit smoke pass added later.
- [x] §8.8 case-study trajectories + demo gif. → §8.8 (T1 + T2 annotated; gifs linked).
- [x] Verify §2 Related-Work cells against 2026-current product docs. → footnoted; A2A→Linux Foundation, AppFunctions=alpha, A3=benchmark not agent.
- [x] Appendix B annotated card. → Amap `hail_ride`.
- [x] Write Abstract.
- [x] Add §8.9 Threats to Validity (baseline cost, eval scope, predictability provenance, safety, cross-round, a11y hit-rate).

Threats-to-validity follow-ups (newly opened, §8.9):
- [x] **Manifest-isolation ablation** — `RELAY_NO_MANIFEST=1` (`agents/relay_agent.py`); n=3 `test-results/ab/nm/`. → §8.9 item 1 (no-manifest ≈14k, splits 19× into ≈5.5× delegation + ≈3.5× manifest).
- [x] **Frugal pure-VLM baseline** — `HISTORY_N_IMAGES=1` (`test-results/ab/n3_hist1/`) + **a11y-text-input baseline** (`agents/a11y_agent.py`, `test-results/ab/a11y/`, n=3; driver `test-results/ab/run_a11y.sh`, fresh-start `benchmark/fresh_conv.py`). → §8.9 item 2 (a11y-text median 47302 = 11.9× RA, only 1.6× under general_e2e; prompt/completion split + $ restatement in §8.2). *(set-of-marks still untested.)*
- [ ] **Reproduce the failure modes** — instrumented runs that trigger premature-exit (seeded stale conversation) and runaway, to put §8.4's sharp variance on tracked data.
- [ ] **Adversarial handoff tests** — assistant auto-submits in one turn / mis-annotated flag / CTA label ≠ `stop_before`; show the contract holds or where it leaks.
- [ ] **Catalog-wide a11y hit-rate** — % taps resolved by uiautomator vs VLM-fallback across the current manifest catalog.
- [ ] **Widen the cost pool** — add a few more tasks to the n=3 instrumented set so headline numbers rest on more than two tasks.
