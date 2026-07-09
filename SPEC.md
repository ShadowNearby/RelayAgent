# RelayAgent Specification

**Version:** 0.1 (draft) · breaking changes expected before 1.0 · Apache-2.0

---

## 1. Motivation

OS-level agents (HarmonyOS Xiaoyi, Apple Intelligence, …) increasingly need to delegate user intents into third-party apps. The two existing paths both fall short: **A2A / App Intents / HMAF** require vendor cooperation that super-apps largely opt out of; **pure GUI agents** drive the full UI surface and are brittle, slow, and legally gray.

This spec defines a third path: a **machine-readable card** describing how an OS agent hands a natural-language task to an explicitly selected app's **own embedded agent** (Amap's AI tab, Yuanbao in WeChat, Xiaohongshu's AI search, …) via a minimal GUI-mediated entry — a short tap sequence to the in-app agent's input field.

The card is **agent-to-agent**, not agent-to-app. The OS agent does not infer the target app from the prompt; the app is selected explicitly by `app_id`. Once a card is selected, the OS agent picks a capability within it, and the in-app agent — which already holds the user's login, preferences, and context — does the work.

## 2. Scope

A `Card` describes:

- **Where** an embedded agent lives inside a host app (entry path).
- **How** to deliver a natural-language prompt to it (invocation surface).
- **What** it can be asked to do (capabilities), each tagged with execution boundary, side-effect class, and handoff requirement.
- **When** the description was last verified against a real device.

A `Card` does **not** describe the host app's general UI graph, internal/private APIs or anything reverse-engineered, or any data the user did not originate.

## 3. File format

- One YAML file per host app: `manifests/<reverse-dns-app-id>.yaml`. UTF-8, LF.
- Top-level keys are fixed; validators MUST reject unknown top-level keys (there is **no** top-level extension point — top-level `x_` keys are invalid).
- Extension keys **below** the top MUST be `x_`-prefixed and MAY be ignored by conforming SDKs. The schema permits `x_` keys in every nested object, and additionally pins the shape of the extensions the reference adapter consumes (§13.1) — so a misspelled step inside `x_post_result_flow` fails validation instead of being silently dropped.

## 4. Top-level schema

```yaml
spec_version: "0.1"          # required, semver of this SPEC
card_version: "1.3.0"        # required, semver of this card
app_id: "com.autonavi.minimap"   # required, primary app id (by convention the Android package)
app_ids:                     # optional, per-platform ids for the same logical app;
  android: "com.autonavi.minimap"  #   android (if present) must equal app_id; keys ⊆ platforms.
  ios: "com.autonavi.amap"         #   Resolution: app_ids[platform], falling back to app_id.
app_name: "Amap"             # required, human-readable
platforms: ["android"]       # required, subset of: android, ios, harmonyos
locale: ["zh-CN"]            # required, BCP-47 tags the embedded agent supports
embedded_agent: { ... }      # required, §5
provenance: { ... }          # required, §9
constraints: { ... }         # required, §10
```

## 5. `embedded_agent` block

```yaml
embedded_agent:
  name: "Amap AI Assistant"          # required, display name of the in-app agent
  description: >                     # required, 1–3 sentences for LLM consumption
    Amap's bottom-center "Press to Talk" tab hosts a built-in AI assistant (text
    and voice). Excels at nearby POI search, route planning, ride hailing, and
    trip planning. Valid only in travel / local-life scenarios.
  entry: { ... }                     # required, §6
  invocation: { ... }                # required, §7
  capabilities: [ ... ]              # required, ≥1, §8
  output: { ... }                    # optional, §7.1
```

## 6. `entry` block

How to reach the embedded agent's input surface from a cold launch.

```yaml
entry:
  primary:
    method: tap_sequence           # only method in v0.1
    steps:
      - tap: { text: "Press to Talk" }
```

Super-apps rarely expose a scheme/intent reaching an internal AI surface, so `tap_sequence` is the only v0.1 entry method. The `primary` wrapper is forward-compatibility: ordered `entry.fallback` paths were dropped for lack of use (OQ-2) but keeping the key makes re-adding them non-breaking.

### 6.1 `method` enum

- **`tap_sequence`** — ordered `steps`, each one of:
  - `tap: { accessibility_id | resource_id | text | text_contains }`
  - `tap_label: { text | text_contains | text_or_desc | text_or_desc_contains | accessibility_id }` — searches the visible screen for a label, scrolling to find it, then taps.
  - `tap: { screen_fraction: { x_ratio, y_ratio } }` — taps at a fractional screen position.
  - `tap_unless_present: { probe, target }` — taps `target` only when `probe` is absent; makes a step idempotent across cold/warm starts.
  - `swipe: up | down | left | right` — scroll direction; compiled to a scroll action.
  - `wait: { ms }` or `wait: { until: { text | text_contains }, timeout_seconds? }` — `timeout_seconds` defaults to 5. The `until` anchor MUST be text-bearing (presence is polled against the a11y dump). A `wait.until` whose anchor never appears is **best-effort**: on timeout the router logs a warning and proceeds; it MUST NOT abort the sequence.
  - `wait_for_reply: { max_seconds?, poll_interval_seconds? }` — wait for the in-app agent to finish. Doneness is implementation-defined (the reference algorithm is §13.1). `max_seconds` defaults to `max(5 × capability.typical_latency_seconds, 60)`.

Selectors MUST prefer, in order: `accessibility_id` > `resource_id` > `text` > `text_contains`.

A selector is normally a **single field**. Multiple fields are allowed only as **alternative anchors for the same node** (e.g. a `screen_fraction` tap point plus the node's hint `text`), resolved by priority — `screen_fraction`, when present, is the authoritative tap point and the rest are hints. This is **not** AND-composition; intersection matching for disambiguation is deferred (OQ-10).

**`screen_fraction` (last-resort selector).** When an element has no usable `resource_id`, `accessibility_id`, or stable `text`, fall back to a screen-relative tap point:

```yaml
trigger:
  screen_fraction: { x_ratio: 0.9051, y_ratio: 0.8690 }
```

Ratios are measured against the visible screenshot's width/height and should point at the visible center of the affordance on the verified device.

**Tap-through on non-clickable anchors** is permitted: routers tap the selected node's center (or declared fraction) and touch dispatch propagates to the nearest clickable ancestor. Prefer the most stable visible text anchor over `screen_fraction` whenever a parent clickable region intercepts the touch.

## 7. `invocation` block

```yaml
invocation:
  input:
    # EditText with no resource-id; hint exposed as `text` while unfocused. The
    # selector is valid only before typing — routers MUST tap the field first,
    # then submit text.
    field: { text: "Ask me anything..." }
  submit:
    # Same ViewGroup is mic (empty) or send (non-empty); no id — screen fraction
    # as last resort.
    trigger:
      screen_fraction: { x_ratio: 0.9051, y_ratio: 0.8690 }
```

The OS agent SHOULD pass the **user's original phrasing** to the embedded agent — the in-app agent is presumed better at interpreting requests in its own domain. The only sanctioned exception is a capability's `prompt_template` (§8.3), where the card author pins the wording. Top-level `locale` informs router-side matching only; it never instructs translation.

### 7.1 `output` block (optional)

```yaml
output:
  method: none                       # none | copy_button
```

`method: none` is the recommended v0.1 default: hand off and stop (result read-back is out of scope, §13). `copy_button` is a core enum value whose locator lives in the `output.x_copy_button` extension (§13.1). `screen_text_extract` and `accessibility_tree` are **reserved** enum values with no v0.1 semantics (OQ-5).

## 8. `capabilities` block

Each capability is a discrete intent. After the card is selected, the router uses `description` + `example_prompts` to pick the best-matching capability.

```yaml
capabilities:
  - id: hail_ride                    # required, snake_case, unique within card
    description: >                    # required, written for an LLM router
      Order a ride via the in-app aggregator. Multi-step: disambiguate drop-off →
      choose car type → confirm estimate → pay. Each step is user-operated.
    example_prompts:                  # required, ≥2, real user phrasings
      - "Call an economy car to the airport"
      - "Get me a ride home"
    executable: true                  # required, §8.1
    handoff_to_user_required: true    # required, §8.2
    typical_latency_seconds: 10       # optional
```

### 8.1 `executable`

- `true` — the in-app agent completes the task end-to-end (subject to user confirmation for irreversible effects).
- `false` — it only **suggests / surfaces / informs**; the router MUST NOT promise the action will be done.

Many in-app "assistants" narrow choices but never close the loop; mislabeling breaks user trust at the OS level.

### 8.2 `handoff_to_user_required`

If `true`, the router MUST return foreground control to the user before the terminal action and MUST NOT auto-tap, auto-confirm, or complete it on the user's behalf. Two sufficient reasons:

1. **Safety** — the action is irreversible or has user-visible cost (payment, message send, delete).
2. **Author intent** — a one-tap CTA (e.g. "Start Navigation") where the user retains a meaningful choice (mode, route, target) that auto-tapping would pre-empt.

Authors SHOULD explain *why* in the `description`. When `executable: false` the flag is vacuous but MUST still be set explicitly (`true` is conventional).

### 8.3 `prompt_template` / `prompt_slots` (optional)

Structured capabilities MAY pin the submitted wording so the router only extracts slot values instead of free-composing (which can derail the app's intent routing):

```yaml
prompt_template: "Navigate to {place}[ by {mode}]."   # {slot} placeholders; optional segments in [...]
prompt_slots:
  - { name: place, desc: "destination name/address", required: true }
  - { name: mode, required: false }                    # optional slots live inside a [...] segment
```

Rules (validated at load time):

- Every `{placeholder}` MUST be a declared slot, and every declared slot MUST be referenced.
- `prompt_slots` without a `prompt_template` is invalid.
- A **required** slot MUST sit outside any `[...]`; a missing value is a hard failure. An **optional** slot MUST sit only inside `[...]`; an empty value drops the whole segment.
- Brackets MUST be balanced and non-nested.

Guarantee boundary: the template fixes *wording / intent routing*; slot **values** are still LLM-extracted and not guaranteed correct. Full conventions: `docs/prompt_template.md`.

## 9. `provenance` block

```yaml
provenance:
  last_verified: "2026-05-10"        # required, ISO 8601
  verified_app_version: "12.4.1"     # required
  verified_os: "android-14"          # required: android-NN | ios-NN | harmonyos-N.N
  verified_device: "Pixel 8"         # optional
  verification_method: manual        # required: manual | scripted | community_reported
  evidence_url: ""                   # optional
  x_device_metrics:                  # optional, verified device metrics (for screen_fraction)
    resolution_px: [1080, 2424]
    density_dpi: 420
```

Tooling marks a card **stale** if `last_verified` is >90 days old, or (where store version is observable) `verified_app_version` is >2 minor versions behind (both thresholds placeholder, OQ-9). Stale cards MUST still be served, marked stale, and SHOULD NOT be used by routers without an explicit override.

## 10. `constraints` block

```yaml
constraints:
  app_version_min: "12.0.0"          # required
  app_version_max: ""                # optional, exclusive upper bound
  region: ["CN"]                     # optional, ISO 3166-1 alpha-2
  network_required: true             # required
  known_issues:                      # optional
    - "First launch triggers location permission dialog; needs pre-handling"
```

## 11. Versioning

- `spec_version` follows this document. **While at 0.x, the minor version carries breaking changes** (semver 0.x convention).
- `card_version` is per-card semver: **major** when capability ids are removed/renamed; **minor** when capabilities/fields are added or `entry`/`invocation` steps change behaviorally (e.g. re-pathing after an app update); **patch** for prose, examples, or `provenance`.
- Routers MUST refuse cards whose `spec_version` major exceeds what they implement; while 0.x, the **minor** version plays that role (a 0.1 router MUST refuse a 0.2 card).

## 12. Conformance

A **conforming card** MUST: (1) validate against `spec/schema.json` (normative); (2) have ≥1 capability, each with ≥2 `example_prompts`; (3) set `provenance.last_verified` to a real dated verification; (4) set `handoff_to_user_required: true` for every capability whose terminal action is irreversible or has user-visible cost.

A **conforming router** MUST: (1) pass user prompts to the embedded agent without semantic rewriting, except when the capability declares a `prompt_template` (§8.3); (2) honor `handoff_to_user_required`; (3) refuse stale cards unless the user opts in.

**Enforcement.** Card rules 1–2 are machine-checked — structure by `spec/schema.json` (layer 1), cross-field consistency (filename↔`app_id`, `platforms`↔`verified_os`, `app_ids`↔`app_id`/`platforms`) by `scripts/validate/validate_manifests.py` (layer 2), and `prompt_template` consistency + capability-id uniqueness at load time (layer 3). Card rules 3–4 (rule 4 needs human judgment about irreversibility) and all router rules are enforced by review and by implementations.

## 13. Out of scope for v0.1

- **Result read-back** from the in-app agent — routers hand off and stop; cross-app aggregation is deferred (OQ-5).
- **Multi-turn sessions** — v0.1 is single-shot (OQ-4).
- **Authentication delegation** — the user's existing in-app login is the trust anchor.
- **Discovery protocol** — v0.1 is a static GitHub registry; a network mechanism is a v0.2 candidate (OQ-6).

### 13.1 `x_` extensions used by the reference adapter

§3 reserves `x_` for extensions conforming SDKs MAY ignore. The reference adapter (`agents/agent/relay_agent.py`) + planner (`agents/agent/action_planner.py`) consume the following in the shipped cards. They are **not normative** — a v0.1 router may ignore them — but cards targeting our adapter rely on them. Promotion to first-class fields is tracked in the open questions.

**Step kinds** (extend §6.1) — `tap_unless_present` and `wait_for_reply` were promoted to core (§6.1). The adapter's `wait_for_reply` doneness is implementation detail: the a11y-tree text hash must hold byte-identical across consecutive dumps within the wall-clock budget; reply text is scraped from the a11y tree, with a VLM reading the frame only when the scrape is empty.

**Capability-level** (extend §8):

- **`x_max_wait_seconds: N`** — override the implicit `wait_for_reply` budget.
- **`x_skip_wait_for_reply: true`** — skip the implicit `wait_for_reply` (for submits that yield a CTA / handoff rather than a text reply, e.g. Amap `navigate_to`).
- **`x_pre_invocation_steps: [<step>, …]`** — steps run after entry but before focusing the input (e.g. lock WPS into "AI PPT" mode).
- **`x_post_result_flow: { steps: [<step>, …] }`** — steps run after `wait_for_reply` returns (e.g. tongyi `order_food` taps through the order sheet, stopping before payment).
- **`x_capture_full_reply: true | { max_scrolls: N }`** — after the reply, scroll and VLM-capture each frame, then stitch (default `max_scrolls=6`; for stacked cards or multi-viewport answers).

**Entry / output**:

- **`entry.x_prepare_fresh_conversation: { description?, steps: [<step>, …] }`** — steps run at each task start (after `open_app`) to clear prior context; disable per-run with `RELAY_FRESH_CONV=0`.
- **`output.method: copy_button`** (extends §7.1) + **`output.x_copy_button: { text?, screen_fraction?, valid_x?, valid_y? }`** — tap the in-app 复制 button so the answer lands on the clipboard. Locator priority: VLM grounding via `text`, then `screen_fraction`, with `valid_x`/`valid_y` as a sanity filter against mis-grounding.

## 14. Relationship to A2A and MCP

The spec reuses concepts from Google's **A2A AgentCard** (`name`, `description`, `capabilities`, `skills`) and Anthropic's **MCP** tool descriptors (rich NL descriptions, structured side-effect metadata), for forward compatibility — a future `to_a2a()` projection should be lossless for the fields A2A expresses. Deliberate differences: RelayAgent is **GUI-mediated by default** (the `entry`/`invocation` blocks are its primary contribution, with no A2A analogue), and it forces explicit `executable` / `handoff_to_user_required` flags because in-app agents are partially capable and routing mistakes have user-visible cost.

## 15. Open questions

Design questions deferred from v0.1. Reference the id (e.g. `OQ-3`) in issues and PRs; ids are stable and never reused (gaps at OQ-13/14 were merged or closed pre-publication).

| # | Question | Status | Target |
| --- | --- | --- | --- |
| OQ-1 | Structured JSON-Schema parameters for capabilities (beyond `prompt_template`) | partial | v0.2 |
| OQ-2 | OCR / vision selectors; reintroduce ordered `entry.fallback` | open | v0.2 |
| OQ-3 | A/B experiments & staged rollouts | leaning no | — |
| OQ-4 | Multi-turn handoff (v0.1 is single-shot) | open | v0.3+ |
| OQ-5 | Standardizing result read-back (adapter does it; spec still says hand-off-and-stop) | partial | v0.2–v0.3 |
| OQ-6 | Discovery / distribution beyond a static registry | open | v0.2 |
| OQ-7 | Trust & card signing / package-signature pinning | open | v0.2 |
| OQ-8 | i18n beyond zh-CN; per-platform `entry`/`invocation` variants | open | — |
| OQ-9 | Keeping cards fresh (90-day rule is a placeholder) | open | — |
| OQ-10 | Composite (intersection) selectors for sibling-shared ids | open | v0.2 |
| OQ-11 | Spatial selectors — relative anchors, dp offsets, cross-device drift guards for `screen_fraction` | partial | v0.2 |
| OQ-12 | `executable` granularity — express orchestrator agents' partner-auth friction tiers | open | v0.2 |
| OQ-15 | Robust selectors for apps that suppress the accessibility tree (super-apps) | partial | v0.2+ |

Most active: `executable` is a boolean that can't express an orchestrator agent's partner-auth friction — pre-authorized vs. one-time OAuth vs. per-session credential all read as `true` (OQ-12); tree-based selectors fail on a11y-suppressed super-apps, partially bridged by `screen_fraction`-only cards but with no cross-device drift guard (OQ-11/15); and result read-back works in the adapter but isn't yet standardized in the normative spec (OQ-5).

---

*End of SPEC v0.1.*
