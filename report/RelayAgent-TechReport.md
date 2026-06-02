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
assistant, Yuanbao in WeChat, Taobao/闪购's assistant, Xiaohongshu's 点点, WPS AI).
The missing piece is therefore not a smarter automation model but a *discovery
layer*: a machine-readable contract that tells an OS-level agent which apps embed
an agent, where its input box is, what it can do, and — critically — when control
must return to the user. We present **RelayAgent**: a per-app card specification, a
GUI-mediated relay adapter that materializes a card into deterministic device
actions, and a `handoff_to_user_required` contract that hands control back before
any irreversible action (payment, ride confirmation, order submission). On a
real-device benchmark over two representative tasks (a food order and a
cross-app discover-then-ride flow), delegating through the in-app agent and
applying two app-agnostic efficiency optimizations (a two-stage reply-completion
precheck and an accessibility-scrape-first text path) reduces VLM token cost by
**2.4–3.6× versus an un-optimized delegation baseline** and by **9.5–11× versus a
pure step-by-step VLM agent driving the same in-app assistant** (and 19–34× versus
a pure-VLM agent driving the native UI by hand), while producing **far more
predictable** cost: RelayAgent's per-task token count is nearly identical across
repetitions (e.g. 3989 / 3950 / 3989 on the food-order task), whereas the pure-VLM
agent ranged across two orders of magnitude (premature termination to runaway
looping) on the identical task.

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
**predictable**, where the pure-VLM baseline's cost varied across two orders of
magnitude run to run.

### 1.1 Contributions

1. **The card specification (SPEC v0.1)** — a machine-readable per-app contract:
   launcher entry path, capability list, example prompts, latency hints, and the
   `handoff_to_user_required` policy, with a JSON-Schema mirror as the normative
   validator. (§4)
2. **A GUI-mediated relay adapter** — materializes a card into deterministic
   device actions under MobileWorld, accessibility-tree-first and
   provider-agnostic across VLMs. (§5)
3. **Multi-app flows + NL routing** — chaining cards across apps with a thin
   text-LLM relay between steps, and a router that picks a single capability or a
   whole flow from natural language. (§6)
4. **Two app-agnostic efficiency optimizations** — a two-stage (perceptual-hash +
   a11y-text-hash) precheck for reply-completion detection, and an
   a11y-scrape-first text-extraction path — with a measured A/B evaluation. (§7,§8)
5. **A four-configuration efficiency study** on a real device, isolating the cost
   of (a) using the in-app agent at all, (b) delegating vs. re-driving the UI, and
   (c) the two optimizations — plus a *predictability* result. (§8)

---

## 2. Related Work

We position RelayAgent as **delegation**, against two existing camps plus one
naming clarification. The comparison table (§2.4) is the spine.

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
agent and returns. The novelty is therefore the discovery layer plus the handoff
contract, not the automation.

[^ma]: X-PLUG/MobileAgent; Mobile-Agent v1 (arXiv:2401.16158), v2
(arXiv:2406.01014, NeurIPS 2024), v3 (arXiv:2508.15144).
[^owl]: GUI-Owl, released with Mobile-Agent-v3 (arXiv:2508.15144); Qwen2.5-VL-based.
[^mobi]: MobiAgent, arXiv:2509.00531 (IPADS-SAI/MobiAgent).
[^appagent]: AppAgent, arXiv:2312.13771 (TencentQQGYLab/AppAgent).
[^autoglm]: AutoGLM, arXiv:2411.00820 (Zhipu); Open-AutoGLM open-sourced 2025-12.
[^mobileuse]: MobileUse, arXiv:2507.16853 (MadeAgents/mobile-use), NeurIPS 2025.
[^a3]: A3: Android Agent Arena, arXiv:2501.01149 — a dynamic evaluation platform
(100 tasks over 20 live apps), used here as a benchmark, not a baseline agent.

Our evaluation makes the contrast concrete and quantitative: we run a pure
step-by-step VLM agent (MobileWorld's `general_e2e`) on the *same* tasks, both
with and without using the app's embedded assistant, and measure the cost gap
(§8). The pure-GUI agent is not only an order of magnitude more expensive but
**far less predictable** — on one task it ranged from a 1-step premature exit to a
50-step runaway, a two-order-of-magnitude spread, while RelayAgent reproduced its
cost nearly identically (3989 / 3950 / 3989 tokens, VLM-calls fixed at 2).

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
| Cost predictability | High | **Low (2-order-of-magnitude spread, §8)** | **High (nearly identical, §8)** |
| Novelty locus | — | Automation model | **Discovery layer + handoff contract** |

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
capability (or, for cross-app intents, a whole flow). The chosen card names a
**launcher entry path** and a **capability** with its slots, latency hint, and
handoff policy. The **relay adapter** (`agents/relay_agent.py`, built on a
planner in `agents/action_planner.py`) materializes that card into a deterministic
action sequence under MobileWorld: cold-launch the app, open a fresh conversation,
type the prompt, wait for the in-app agent's reply to complete, scrape the reply
text, and — when the capability is marked irreversible — hand control back to the
user. **Flows** (`agents/flow_runner.py`, `manifests/_flows/*.yaml`) chain cards
across apps, relaying each step's reply forward through a small text-LLM extract.
A shared adb helper (`agents/_adb.py`) backs cold-launch, swipes, and device
selection.

The remainder of the paper details the card spec (§4), the adapter mechanisms
(§5), flows and routing (§6), the two efficiency optimizations (§7), and the
benchmark (§8).

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
  `x_bounds`).
- **Card class.** Single-bubble (the whole reply renders in one TextView) vs.
  multi-node list (a RecyclerView of cards). This dichotomy drives extraction
  strategy (§5.4).

### 4.1 The handoff contract

The normative core of the spec is `handoff_to_user_required`. A capability marked
true must, in the adapter, **emit an `ask_user` and return control before any
irreversible call-to-action** — payment, ride confirmation, order submission. This
is what makes RelayAgent *delegation* rather than *automation*: the in-app agent
does the reversible preparation (assemble the cart, set the destination, draft the
order), and the human authorizes the irreversible step. In the benchmark, every
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
by **label, not package** — see §4.) Cold launch is not a detail: §8.4 shows that
a pure-VLM agent *without* a fresh-conversation step misread stale on-screen state
and terminated prematurely; RelayAgent's explicit fresh-conversation step
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

## 6. Multi-App Flows and Natural-Language Routing

**Flows** (`agents/flow_runner.py`, `manifests/_flows/*.yaml`) chain cards across
apps. Each step cold-launches its app, pins one capability, captures the reply,
and relays it forward through a small text-LLM `extract` that pulls structured
fields (e.g. a candidate list) into the flow blackboard. An `ask_user` step lets
the user choose among candidates. Reference flows: `xhs_to_amap_place` (find
niche/new places in Xiaohongshu, pick one, hail an Amap ride there) and
`wechat_to_wps_summary`. Each sub-step runs as its own MobileWorld invocation under
a per-leg `--log-file-root`, so per-leg trajectories and token counts are
recoverable (used throughout §8).

**NL routing** (`scripts/run_nl.py` + `agents/capability_router.py`) builds a
catalog of all cards and flows and asks the text LLM to pick the best executor —
a single capability or a whole flow — before dispatch, with a `--dry-run` mode for
routing inspection. The motivating end-to-end case is the `xhs_to_amap_place` demo
("在上海找三家评价好的小众书店，挑一家打车过去").

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
separately in §8.2.1: each of the 28 capabilities in the 7 cards was driven
end-to-end at least once and reached its expected terminal state (a completed
reply for informational capabilities, the correct pre-CTA handoff screen for
irreversible ones). These are author-run functional passes (n=1 per capability),
not the instrumented n=3 cost runs.

| App | Package | Capabilities | Card class |
| --- | --- | --- | --- |
| Amap | com.autonavi.minimap | POI search, navigation, ride hailing, trip planning | mixed |
| Tongyi Qwen | com.aliyun.tongyi | chat, train/ride/food/hotel/movie booking | mixed |
| Ctrip | ctrip.android.view | flights, hotels, trains, attractions, packages | mixed |
| Xiaohongshu | com.xingin.xhs | community Q&A via AI search | multi-node |
| Taobao | com.taobao.taobao | product search, compare, purchase, order tracking | multi-node |
| WeChat | com.tencent.mm | Yuanbao chat, AI search | mixed |
| WPS Office | cn.wps.moffice_eng | AI doc/PPT/writing | single-bubble |

#### 8.2.1 Capability coverage (28 capabilities across 7 cards)

Every capability in the catalog was exercised end-to-end and reached its
expected terminal state. "Handoff" marks capabilities with
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
| 通义千问 | order_food | ✓ | ✓ |
| 通义千问 | hail_ride | ✓ | ✓ |
| 通义千问 | book_hotel | ✓ | ✓ |
| 通义千问 | book_movie | ✓ | ✓ |
| 携程旅行 | chat_travel_qa | — | ✓ |
| 携程旅行 | search_flight | ✓ | ✓ |
| 携程旅行 | search_hotel | ✓ | ✓ |
| 携程旅行 | search_train | ✓ | ✓ |
| 携程旅行 | plan_trip | ✓ | ✓ |
| 携程旅行 | search_attraction_info | — | ✓ |
| 小红书 | qa_community_knowledge | — | ✓ |
| 淘宝 | search_product | — | ✓ |
| 淘宝 | compare_products | — | ✓ |
| 淘宝 | buy_product | ✓ | ✓ † |
| 淘宝 | order_local_delivery | ✓ | ✓ † |
| 淘宝 | track_order | — | ✓ |
| 微信 | ai_search | — | ✓ |
| WPS Office | chat | — | ✓ |
| WPS Office | ai_ppt | ✓ | ✓ |
| WPS Office | quick_writing | ✓ | ✓ |
| WPS Office | doc_reading | ✓ | ✓ |
| WPS Office | web_summary | — | ✓ |

**Coverage: 28/28 capabilities reached their expected terminal state.** 16 of
the 28 are `handoff_to_user_required` and every one stopped at the correct
pre-CTA screen (cf. §8.6).

> † **Taobao risk-control caveat.** Taobao enforces strict server-side
> risk-control: scripted GUI interaction on `buy_product` /
> `order_local_delivery` can trip an account/device-level "访问被拒绝" wall or a
> one-time identity-verification gate, **and repeated automated operation risks
> the account being flagged or banned**. The entry path and handoff behavior are
> sound (both reached the pre-CTA screen on a clean account), but this is the one
> card where GUI-mediated relay carries a standing account-safety risk that a
> vendor-API path (§2.1) would avoid. We mark these ✓ for functional coverage
> while flagging the operational hazard; see §9.

### 8.2 Token cost (the headline result)

Median token cost per task (n=3 for RA / general_e2e; manual-UI n=1):

**T1 — order_food** (same Taobao-闪购 / Qwen backend, interaction style varies):

| Configuration | Median tokens | Token runs (n=3) | VLM calls | vs RA optimized |
| --- | ---: | --- | ---: | ---: |
| MW manual-UI (no assistant) | 75463 | (n=1) | — | 18.9× |
| MW general_e2e (uses assistant) | ~38k* | 38282 / 77347 / 96888 | — | ~9.5× |
| RA baseline | 9566 | 6790 / 9566 / 12423 | 3–5 (med 4) | 2.4× |
| **RA optimized** | **3989** | 3989 / 3950 / 3989 | **2** | **1×** |

\* general_e2e on T1 is **not stable** (see §8.4); the clean-run cost is ~38k
(this round's r1 = 38282; Round-1 = 38081), used as its representative value.

**T2 — flow** (xhs→amap, sum of discover + ride legs):

| Configuration | Median tokens | Token runs (n=3, total) | vs RA optimized |
| --- | ---: | --- | ---: |
| MW manual-UI (no assistant) | 294695 | (n=1) | 34.0× |
| MW general_e2e (uses assistant) | 95296 | 95296 / 95273 / 103988 | 11.0× |
| RA baseline | 31174 | 34247 / 22535 / 31174 | 3.6× |
| **RA optimized** | **8662** | 8662 / 5705 / 11233 | **1×** |

Reading the gradients answers Q1–Q3:

- **Q1 (use the in-app agent at all).** manual-UI → general_e2e: 75463 → ~38k
  (−50%) on T1, 294695 → 95296 (−68%) on T2. Merely *using* the app's assistant
  collapses a long native-UI navigation into one conversational turn. On T2's
  discover leg this is starkest: finding restaurants took **23 native-UI steps**
  (scrolling notes) by hand vs. **7 steps** with the assistant.
- **Q2 (delegate vs. re-drive).** general_e2e → RA optimized: **−89.5% / 9.5×** on
  T1 and **−90.9% / 11.0×** on T2. The gap is the cost of re-deriving every step
  from full screenshots (general_e2e also re-sends a 3-image visual history each
  step) versus RelayAgent's structured plan with zero-token a11y taps and a
  discovery manifest that pins the assistant's entry path.
- **Q3 (the two optimizations).** RA baseline → RA optimized: **−58.3% / 2.4×** on
  T1 and **−72.2% / 3.6×** on T2, from precheck (fewer done-detection VLM polls:
  T1 VLM 4→2) and scrape (zero-token text extraction).

### 8.3 Wall-clock — an honest negative result

| Config | T1 wall_s (median) | T2 wall_s (median) |
| --- | ---: | ---: |
| RA optimized | 74.1 | 153.5 |
| RA baseline | 47.6 | 115.8 |
| MW general_e2e | 111 (46 / 111 / 379) | 166 |
| MW manual-UI | 193 (n=1) | 717 (n=1) |

The optimizations of §7 are a **token/cost** win, **not** a wall-clock win at the
RA-baseline→RA-optimized step: the two configs' medians sit within run-to-run
noise and the optimized config is in fact *higher* here (T1 74.1 vs 47.6 s; T2
153.5 vs 115.8 s). The cause is structural — end-to-end latency is **dominated by
(a) the in-app assistant's own generation time and (b) per-call LLM-gateway
latency** (the single done-confirming VLM poll ranged 6–43 s across reps, pure
gateway jitter). Precheck/scrape remove VLM *calls and tokens*, not the wait for
the assistant to finish. We report this plainly rather than claim time savings the
data does not support. Wall-clock savings *do* appear at the coarser gradient: a
pure-VLM agent re-driving the UI is materially slower (T2 166 s vs 154 s), and its
failure modes are far slower still (T1 runaway: 379 s; manual-UI T2: 717 s).

> Note: the per-step **sleep trims** (perf-trim: `step_wait_time 1.0→0.2`,
> `MW_WAIT_SECONDS 1.0→0.2`, poll-skip `0.8→0.3`) *are* a wall-clock win, but that
> is a before/after comparison of the **same optimized config** (order_food
> cold-launch→handoff ~70 s → ~51 s; poll tick 4.0 s → 1.8 s), a different axis
> from the optimized-vs-baseline comparison above. Do not conflate the two.

### 8.4 Predictability — RelayAgent is deterministic-ish; the pure-VLM agent is not

The most striking n=3 finding is variance. RA token counts are nearly identical
across repetitions — T1 optimized **3989 / 3950 / 3989** (VLM fixed at 2),
baseline **6790 / 9566 / 12423** (VLM 3/4/5, each extra done-poll ≈ +3k). The
pure-VLM `general_e2e` on the *same* T1 produced **38282 / 77347 / 96888 tokens at
46 / 111 / 379 s** — driven by two opposite failure modes:

- **Premature termination:** after cold launch the Qwen app restores a prior
  conversation/order card; lacking a fresh-conversation step, the agent reads it
  as done and exits in 1–2 steps.
- **Runaway:** the agent never recognizes completion and loops toward the step
  cap, re-sending the full screenshot + 3-image history each step (in Round 1 this
  ran to a 50-step / 531,597-token tail).

The lesson is a system one: RelayAgent's explicit fresh-conversation step
(`x_prepare_fresh_conversation`) and structured plan make its cost *predictable*,
which a pure-VLM screenshot-to-action loop is not. For an operator paying per
token, a predictable ~4 k beats a 38 k–530 k lottery. **Predictable cost is itself
a system contribution.**

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

Two end-to-end trajectories make the cost numbers concrete. Demo recordings:
`assets/RelayAgentDemoOrder/RelayAgentDemoOrder.gif` (T1) and
`assets/RelayAgentDemoFlow/RelayAgentDemoFlow.gif` (T2). Step strings below are
verbatim from the run `prediction` fields in `traj.json`.

#### 8.8.1 T1 order_food — RA optimized (run `order_food_optimized_r1`, 3989 tokens, 2 VLM calls)

The 11-step plan expanded to 16 runner steps (the single `wait_for_reply` step
held for 6 of them — see §5.3's non-advancing semantics). Only **two** LLM calls
were made in the entire task: one text-only `capability_router` pick (1054→75
tok) and one `reply_watch` done-judgment (2783→77 tok). Reply text was scraped
from the a11y tree at zero token cost.

| Plan step | Action | Note |
| --- | --- | --- |
| 1/11 | tap_fraction | fresh conversation |
| 2/11 | tap_text | fresh conversation (uiautomator hit) |
| 3/11 | wait_text | `'发消息或按住说话…'` present (2471 ms) |
| 4/11 | tap_text | focus input (uiautomator) |
| 5/11 | input_text | `帮我点三杯蜜雪冰城蜜桃四季春，温度和糖度都用默认` |
| 6/11 | tap_bounds | submit |
| 7/11 | wait_for_reply | **precheck skip ×5** (screen changing, 0.0→7.3 s), then **1 VLM poll → done** @ ~9 s |
| 8/11 | tap_text | select store (uiautomator) |
| 9/11 | tap_text | add to cart (uiautomator) |
| 10/11 | wait_ms | settle |
| 11/11 | **handoff** | `ask_user` carrying the scraped reply, stops at the order/payment screen |

The captured reply handed to the user: *"已为你找到附近多家蜜雪冰城门店的蜜桃四季春，
请选择你想要下单的店铺… 选好后我帮你加购3杯，温度和糖度都用默认～"*. This is the
canonical low-cost path: the two-stage precheck (§7.1) collapsed the streaming
window into a single done-poll, and the a11y scrape (§7.2) kept text extraction
at zero VLM calls — hence the near-constant 3989 / 3950 / 3989 token figure
(§8.4).

For contrast, `general_e2e` on the identical task and backend spent
**38282–96888 tokens over 5–11 steps** in the same n=3 round, re-deriving each
step (cart, store pick, quantity) from full screenshots plus a 3-image visual
history — an order of magnitude more, with two-order-of-magnitude variance.

#### 8.8.2 T2 flow — RA optimized (run `flow_optimized_r1`, 8662 tokens total)

The cross-app flow ran as two independently-logged MobileWorld legs (§6):

- **Leg 1 — discover (Xiaohongshu `qa_community_knowledge`, 5853 tok).** 6-step
  plan → 15 runner steps. After submit, precheck skipped 5 streaming ticks, the
  VLM confirmed `done`, then the `x_capture_full_reply` loop scrolled the
  RecyclerView **3 of 6** allotted scrolls, scraping each frame (zero VLM text
  calls), and stitched the restaurant list. The flow's text-LLM `extract` then
  pulled the candidate list into the blackboard.
- **Leg 2 — ride (Amap `hail_ride`, 2809 tok).** 9-step plan → 16 runner steps,
  including the `tap_unless_present` conditional that flips the AI tab from voice
  to text mode (probe `有什么问题尽管问我` absent → tap keyboard glyph). Same
  precheck-then-capture pattern (1 scroll), ending at the `立即打车` handoff
  screen — no confirm tap (§8.6).

Both legs reached their terminal state with **zero per-leg VLM grounding calls**
(every tap resolved through uiautomator), which is why the whole two-app flow
cost less than a *single* general_e2e order leg. The discover leg alone replaced
the **23 native-UI steps** a manual run needed to scroll notes by hand (§8.2,
Q1) with 6 plan steps.

---

## 9. Limitations & Known Blockers

- **Sample size & scope.** The benchmark covers two tasks at n=3 (RA /
  general_e2e); MW manual-UI is n=1. Full 7-card / per-capability success rates
  (§8.2) are not yet measured. T1's manual leg uses a different host app (Taobao)
  than the assistant legs (Qwen), though the fulfillment backend is shared.
- **Taobao server-side risk-control wall.** `buy_product` /
  `order_local_delivery` can hit an account/device-level "访问被拒绝" wall (and a
  one-time identity-verification gate we hit mid-benchmark); this is not an adapter
  bug — the entry path is sound. More seriously, **scripted GUI interaction on
  Taobao risks the account being flagged or banned** by its anti-automation
  risk-control. This is the one card where the GUI-mediated path carries a standing
  account-safety hazard that a vendor-API route (§2.1) would sidestep; the
  Qwen-hosted `order_food` path (§8.1) goes through the same fulfillment backend
  without driving the Taobao app directly, and is the safer way to exercise it.
- **VLM grounding fallback** accuracy degrades on CN UIs when the a11y tree is
  empty (WebView-rendered content); Qwen-VL's normalized-vs-pixel coordinate
  ambiguity is handled by a heuristic in `_ground_text`.
- **Card maintenance.** Entry paths can drift as apps redesign their UI.
- **Scope.** GUI-mediated relay only — no endpoint, no data scraping beyond the
  on-screen reply.

---

## 10. Conclusion & Future Work

RelayAgent's contribution is a *discovery layer + handoff contract* for delegating
to apps' own logged-in AI agents, not another automation model. The benchmark
shows the practical payoff of delegation: an order-of-magnitude token reduction
versus a pure-VLM agent on the same task and the same in-app assistant, a further
2.4–3.6× from two app-agnostic optimizations, and — the result we did not anticipate —
**predictable** per-task cost where the pure-VLM baseline varied across two orders
of magnitude. We are deliberate about what the optimizations buy: token/cost, not
wall-clock (which the in-app assistant's own latency dominates).

Future work: more cards (and OEM-published cards); richer handoff semantics;
cross-app flow planning; non-Android platforms; and **A2A forward-compatibility** —
when apps ship endpoints, cards become a thin shim or disappear (SPEC §14).

---

## Appendix A. Reproducibility

- **Entry points.** `scripts/run_test.py <pkg> "<goal>"` (single app),
  `scripts/run_flow.py <flow.yaml> --nl "<goal>"` (multi-app),
  `scripts/run_nl.py "<goal>"` (routed). All cold-launch the target and set
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
            target: { x_bounds: { box: [946, 2101, 1009, 2164], anchor: bottom_right } }
        - wait: { ms: 300 }
    fallback: []

  # ── Where the prompt goes / how it is sent ──
  invocation:
    input:
      field:                            # EditText has no id/content-desc;
        x_bounds: { box: [68, 2105, 925, 2160], anchor: bottom_left }
        text: "有什么问题尽管问我"        # hint text works only pre-focus
      max_chars: 500
    submit:
      trigger:                          # same ViewGroup: mic when empty, send-arrow when typed
        x_bounds: { box: [946, 2075, 1009, 2138], anchor: bottom_right }
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
- [x] Fill §8.2 (token), §8.3 (time, incl. the negative result), §8.4 (predictability), §8.5 (quality).
- [x] Re-run n=3 post latency-trim + fork robustness patches (2026-06-02); refresh §8.2–8.6, add §8.7 robustness.
- [x] §8.2 full 7-card / per-capability success-rate table. → §8.2.1 (28/28).
- [x] §8.8 case-study trajectories + demo gif. → §8.8 (T1 + T2 annotated; gifs linked).
- [x] Verify §2 Related-Work cells against 2026-current product docs. → footnoted; A2A→Linux Foundation, AppFunctions=alpha, A3=benchmark not agent.
- [x] Appendix B annotated card. → Amap `hail_ride`.
- [x] Write Abstract.
