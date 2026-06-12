# RelayAgent Specification

**Version:** 0.1 (draft)
**Status:** Working draft — breaking changes expected before 1.0
**License:** Apache-2.0

---

## 1. Motivation

Mobile OS-level agents (HarmonyOS Xiaoyi, Apple Intelligence, etc.) increasingly need to delegate user intents into third-party apps. Two existing paths each have problems:

- **A2A / App Intents / HMAF** — require explicit cooperation from app vendors; super-apps largely opt out or expose only marginal capabilities.
- **Pure GUI agents** — operate at the app's full UI surface, are brittle to UI changes, slow, and legally gray.

This spec defines a third path: a **machine-readable card** that describes how an OS-level agent can hand off a natural-language task to an explicitly selected app's **own embedded agent** (the AI tab inside Amap, Yuanbao inside WeChat, Xiaohongshu's AI search, etc.) via a minimal, GUI-mediated entry — a short tap sequence to reach the in-app agent's input field.

The card is intentionally **agent-to-agent**, not agent-to-app. The OS agent does not infer the target app from the prompt alone; the target app is selected explicitly by package name / bundle id (`app_id`). Once a card is selected, the OS agent chooses the relevant capability within that card. The in-app agent — which already has the user's login, preferences, and full app context — does the work.

## 2. Scope

A `Card` describes:

- **Where** an embedded agent lives inside a host app (entry path).
- **How** to deliver a natural-language prompt to it (invocation surface).
- **What** it can be reasonably asked to do (capabilities), each tagged with execution boundary, side-effect class, and handoff requirements.
- **When** the description was last verified against a real device.

A `Card` does **not** describe:

- The host app's general UI graph.
- Internal APIs, private endpoints, or anything obtained by reverse engineering.
- Any data the user did not originate.

## 3. File format

- One YAML file per host app: `manifests/<reverse-dns-app-id>.yaml`.
- UTF-8, LF line endings.
- Top-level keys are fixed; unknown top-level keys MUST be rejected by validators (this includes top-level `x_` keys — there is no extension point at the top level).
- Extension keys at any level **below** the top MUST be prefixed with `x_` and MAY be ignored by conforming SDKs. The schema permits `x_` keys inside every nested object. For the extensions the reference adapter consumes (§13.1), the schema additionally pins their shape — a misspelled step inside `x_post_result_flow` fails validation instead of being silently dropped at runtime.

## 4. Top-level schema

```yaml
spec_version: "0.1"          # required, string, semver of this SPEC
card_version: "1.3.0"        # required, semver of this card file
app_id: "com.autonavi.minimap" # required, primary app id (by convention the Android package name)
app_ids:                      # optional, per-platform ids for the same logical app;
  android: "com.autonavi.minimap"  #   `android` (if present) must equal app_id, keys ⊆ platforms.
  ios: "com.autonavi.amap"         #   Resolution: app_ids[platform], falling back to app_id.
app_name: "Amap"              # required, human-readable
platforms: ["android"]       # required, subset of: android, ios, harmonyos
locale: ["zh-CN"]            # required, BCP-47 tags the embedded agent supports
embedded_agent:              # required, see §5
  ...
provenance:                  # required, see §9
  ...
constraints:                 # required, see §10
  ...
```

## 5. `embedded_agent` block

```yaml
embedded_agent:
  name: "Amap AI Assistant"          # required, display name of the in-app agent
  description: >                     # required, 1–3 sentences for LLM consumption
    Amap's bottom-center "Press to Talk" tab hosts a built-in AI assistant
    supporting text and voice input. Excels at nearby POI search, route planning,
    multi-step ride hailing, and multi-day trip planning. Only valid within
    travel and local-life scenarios; explicitly does not support out-of-domain
    actions such as phone calls.

  entry:                             # required, see §6
    ...
  invocation:                        # required, see §7
    ...
  capabilities:                      # required, ≥1, see §8
    - ...
  output:                            # optional, see §7.1
    ...
```

## 6. `entry` block

Describes how to reach the input surface of the embedded agent from a cold app launch.

```yaml
entry:
  primary:
    method: tap_sequence           # tap_sequence (only method in v0.1)
    steps:
      - tap: { text: "Press to Talk" }  # bottom-center tab; no deep link exposed
```

Super-apps generally do not expose a public scheme or intent that reaches an internal AI surface, so `tap_sequence` is the only entry method in v0.1. (Deep-link / intent methods were specified in early drafts but no real card used them; see §13.)

The `primary` wrapper is deliberate forward-compatibility: ordered fallback paths (`entry.fallback`) were dropped from v0.1 for lack of use but are a v0.2 candidate (see §15 / OQ-2), and keeping the key means adding them back is non-breaking.

### 6.1 `method` enum

- **`tap_sequence`** — ordered list of `steps`, each one of:
  - `tap: { accessibility_id | resource_id | text | text_contains }`
  - `tap_label: { text | text_contains | text_or_desc | text_or_desc_contains | accessibility_id }` — searches the visible screen for a label, scrolling to find it, then taps.
  - `tap: { screen_fraction: { x_ratio, y_ratio } }` — taps at a fractional screen position.
  - `tap_unless_present: { probe: <selector>, target: <selector> }` — taps `target` only when `probe` is absent; makes an entry step idempotent across cold/warm starts.
  - `swipe: up | down | left | right` — scroll direction; compiled to a scroll action.
  - `wait: { ms }` or `wait: { until: { text | text_contains }, timeout_seconds? }` — `timeout_seconds` defaults to 5. The `until` anchor MUST be text-bearing (`text` or `text_contains`): presence is polled against the a11y dump, so coordinate or id-only selectors are not pollable. A `wait.until` whose anchor never appears is **best-effort**: on timeout the router logs a warning and proceeds to the next step; it MUST NOT abort the sequence.
  - `wait_for_reply: { max_seconds?, poll_interval_seconds? }` — wait for the in-app agent to finish responding before continuing. How doneness is decided is implementation-defined (the reference adapter's deterministic algorithm is described in §13.1). `max_seconds` defaults to `max(5 × capability.typical_latency_seconds, 60)`.

Selectors MUST prefer, in order: `accessibility_id` > `resource_id` > `text` > `text_contains`.

A selector is normally a **single field**. A selector MAY carry multiple fields only when they are **alternative anchors for the same node** (e.g. a `screen_fraction` tap point plus the node's hint `text` as a semantic anchor); routers resolve such selectors by priority — `screen_fraction`, when present, is the authoritative tap point, and the remaining fields are hints/fallbacks for matchers that want a semantic anchor. Multi-field selectors are **not** AND-composites: intersection matching for disambiguation (`{ resource_id, text }` must match both) is not in v0.1 and is tracked in OQ-10.

**Screen fraction as a last-resort selector (`screen_fraction`).** When an element has neither a usable `resource_id`, `accessibility_id`, nor stable `text`, the author MAY fall back to a screen-relative tap point:

```yaml
trigger:
  screen_fraction:
    x_ratio: 0.9051
    y_ratio: 0.8690
```

Ratios are measured against the visible screenshot width and height. They should point at the visible center of the target affordance on the verified device.

**Tap-through on non-clickable anchors.** Selecting a non-clickable node (e.g. a TextView label, a placeholder hint) is explicitly permitted. Routers perform the tap at the selected node's center or declared screen fraction; touch dispatch propagates the event to the nearest clickable ancestor. Card authors SHOULD prefer the most stable visible text anchor over `screen_fraction` whenever the parent clickable region intercepts the touch.

## 7. `invocation` block

```yaml
invocation:
  input:
    # EditText with no resource-id; placeholder/hint exposed as `text` while
    # unfocused. Selector is valid only before the user starts typing — so
    # routers MUST tap the field first, then submit text, in that order.
    field: { text: "Ask me anything..." }
  submit:
    # Same ViewGroup acts as mic (empty input) or send (non-empty input).
    # No id / no content-desc — screen fraction recorded as last resort.
    trigger:
      screen_fraction:
        x_ratio: 0.9051
        y_ratio: 0.8690
```

The OS agent SHOULD pass the **user's original phrasing** to the embedded agent whenever possible. Rewriting the user's prompt before handoff defeats the design — the in-app agent is presumed better at interpreting requests in its own domain. The one sanctioned exception is a capability-declared `prompt_template` (§8.3), where the card author — not the router — has pinned the wording.

The top-level `locale` list declares which languages the embedded agent accepts. It informs router-side card/capability matching only; it does not instruct the router to translate — the submitted prompt stays in the user's own language per the rule above.

### 7.1 `output` block (optional)

```yaml
output:
  method: none                       # none | copy_button
```

`method: none` is the recommended default for v0.1: the OS agent hands off and stops. Reading results back is explicitly out of scope for this version (see §13). `copy_button` is a core enum value, but its locator details live in the `output.x_copy_button` extension consumed by the reference adapter (see §13.1).

`screen_text_extract` and `accessibility_tree` are **reserved** enum values: they name read-back methods whose semantics will be defined by the OQ-5 standardization and are intentionally not valid in v0.1 (no shipped card uses them, and a v0.1 router would have no defined behavior to attach to them).

## 8. `capabilities` block

Each capability is a discrete intent the embedded agent can be asked to fulfill. After the target app/card has been explicitly selected, the OS-level router uses `description` and `example_prompts` to decide which capability best matches the user request.

```yaml
capabilities:
  - id: hail_ride                    # required, snake_case, unique within card
    description: >                    # required, written for an LLM router
      Order a ride via the in-app ride-hailing aggregator. Multi-step flow:
      disambiguate drop-off → choose car type → confirm estimate → pay.
      Each step is user-operated within the agent's response card.
    example_prompts:                  # required, ≥2, real user phrasings
      - "Call an economy car to the airport"
      - "Get me a ride home"
    executable: true                  # required, see §8.1
    handoff_to_user_required: true    # required, see §8.2
    typical_latency_seconds: 10       # optional
```

### 8.1 `executable`

- `true` — the in-app agent can complete the task end-to-end (subject to user confirmation for irreversible side effects).
- `false` — the agent only **suggests / surfaces / informs**. The OS router MUST NOT promise the user that the action will be done.

This distinction matters: many in-app "AI assistants" narrow choices but never close the loop. Mislabeling them breaks user trust at the OS level.

### 8.2 `handoff_to_user_required`

If `true`, the OS router MUST return foreground control to the user before the capability's terminal action and MUST NOT auto-tap, auto-confirm, or otherwise complete the action on the user's behalf.

Two distinct reasons to set this `true`:

1. **Safety** — the action is irreversible or has user-visible cost (payment, message send, delete). Auto-confirming is unsafe.
2. **Author intent** — the in-app agent presents a one-tap CTA (e.g. a "Start Navigation" button) where the user retains meaningful choice (mode, route, target). Auto-tapping pre-empts a choice the user expects to make.

Either reason is sufficient. Card authors are encouraged to explain *why* `true` in the capability's `description`.

When `executable: false`, the flag is vacuous — there is no terminal action for the router to complete, so control always returns to the user. Authors MUST still set it explicitly (the schema requires it); `true` is the conventional value.

### 8.3 `prompt_template` / `prompt_slots` (optional)

Structured capabilities (navigation, booking, messaging, …) MAY pin the wording of the prompt sent to the in-app agent, so an LLM router only extracts slot values instead of free-composing the prompt (which can derail the app's intent routing):

```yaml
prompt_template: "Navigate to {place}[ by {mode}]."   # `{slot}` placeholders; optional segments in `[...]`
prompt_slots:
  - name: place                       # required: snake_case slot name
    desc: "destination name/address"  # optional: hint for the slot extractor
    required: true                    # default true
  - name: mode
    required: false                   # optional slots MUST sit inside a `[...]` segment
```

Rules (validated at load time by conforming routers):

- Every `{placeholder}` MUST be a declared slot; every declared slot MUST be referenced.
- `prompt_slots` without a `prompt_template` is invalid (schema-enforced) — slots describe a template, never stand alone.
- A **required** slot MUST appear outside any `[...]` segment; missing value = hard failure.
- An **optional** slot MUST appear only inside `[...]`; an empty value drops the whole segment (including surrounding wording).
- Brackets MUST be balanced and non-nested.

The guarantee boundary: the template fixes *wording/intent routing*; slot **values** are still LLM-extracted and not guaranteed correct. Full conventions and worked examples: `docs/prompt_template.md`.

## 9. `provenance` block

```yaml
provenance:
  last_verified: "2026-05-10"        # required, ISO 8601 date
  verified_app_version: "12.4.1"     # required
  verified_os: "android-14"          # required: "android-NN" | "ios-NN" | "harmonyos-N.N"
  verified_device: "Pixel 8"         # optional
  verification_method: manual        # required: manual | scripted | community_reported
  evidence_url: ""                   # optional, link to script / video / screenshots
  x_device_metrics:                  # optional, records the verified screenshot/device metrics
    resolution_px: [1080, 2424]
    density_dpi: 420
```

A card is considered **stale** by tooling if `last_verified` is more than 90 days old, or — where the tooling can observe the current store version — if `verified_app_version` is more than two minor versions behind it. (Both thresholds are placeholders until real freshness signal exists; see OQ-9.) Stale cards MUST still be served by the registry, marked as stale, and SHOULD NOT be used by routers without an explicit override.

## 10. `constraints` block

```yaml
constraints:
  app_version_min: "12.0.0"          # required
  app_version_max: ""                # optional, exclusive upper bound
  region: ["CN"]                     # optional, ISO 3166-1 alpha-2
  network_required: true             # required
  known_issues:                      # optional
    - "First launch triggers location permission dialog; needs pre-handling"
    - "Entry button invisible when not logged in"
```

## 11. Versioning

- `spec_version` follows this document. Breaking changes bump major. **While the SPEC is at 0.x, the minor version carries breaking changes** (standard semver 0.x convention).
- `card_version` is per-card semver. Bump **major** when capability ids are removed or renamed; **minor** when capabilities or fields are added, **or when `entry` / `invocation` steps or selectors change behaviorally** (e.g. re-pathing after an app update); **patch** for prose, examples, or `provenance` updates.
- Routers MUST refuse cards whose `spec_version` major exceeds the version they implement; while `spec_version` is 0.x, the **minor** version plays this role (a 0.1 router MUST refuse a 0.2 card).

## 12. Conformance

A **conforming card** MUST:

1. Validate against the schema (JSON Schema mirror at `spec/schema.json`, normative).
2. Have at least one capability, and **every** capability has at least two `example_prompts` (the schema enforces this per-capability).
3. Have `provenance.last_verified` set to a real, dated verification.
4. For every capability whose terminal action is irreversible or has user-visible cost (payment, message send, delete), set `handoff_to_user_required: true`.

A **conforming router** MUST:

1. Pass user prompts to the embedded agent without semantic rewriting — except when the selected capability declares a `prompt_template` (§8.3), in which case the template governs the wording and the router only extracts slot values from the user's utterance.
2. Honor `handoff_to_user_required`.
3. Refuse to use cards marked stale unless the user explicitly opts in.

**Enforcement layers.** Card rules 1–2 are machine-checked: structure by `spec/schema.json` (layer 1), cross-field consistency (filename ↔ `app_id`, `platforms` ↔ `verified_os`, `app_ids` ↔ `app_id`/`platforms`) by `scripts/validate_manifests.py` (layer 2), and `prompt_template` consistency plus capability-id uniqueness at catalog load time (layer 3). Card rules 3–4 and all router rules are **not machine-checkable** — they are enforced by card review (rule 4 needs a human judgment about irreversibility) and by router implementations respectively.

## 13. Out of scope for v0.1

- **Result read-back** from the in-app agent. Routers hand off and stop; cross-app aggregation (e.g. price comparison) is deferred.
- **Multi-turn sessions** with the embedded agent. v0.1 is single-shot.
- **Authentication delegation** between OS agent and in-app agent. The user's existing in-app login is the trust anchor.
- **Discovery protocol.** v0.1 ships as a static GitHub registry; a network discovery mechanism (à la `.well-known/agent-card.json`) is a candidate for v0.2.

## 13.1 `x_` extensions used by the reference adapter

SPEC §3 (File format) reserves the `x_` prefix for vendor/implementation
extensions that conforming SDKs MAY ignore. The reference adapter
(`agents/relay_agent.py`) + planner (`agents/action_planner.py`) consume
the following extensions in the ten shipped reference cards. They are
**not normative** — a v0.1-conforming router is free to ignore any of them
— but card authors targeting our adapter rely on them. Promotion to first-
class fields is tracked in `SPEC-OPEN-QUESTIONS.md`.

### Step kinds (extend §6.1)

(`tap_unless_present` and `wait_for_reply` were both promoted from
extensions to core step kinds; see §6.1. The adapter's `wait_for_reply`
doneness algorithm is implementation detail, not normative: the a11y-tree
text hash must hold byte-identical across consecutive dumps, within the
wall-clock budget; the reply text is scraped from the a11y tree, and a VLM
only reads the frame when the scrape comes up empty.)

### Capability-level extensions (extend §8)

- **`x_max_wait_seconds: N`** — per-capability override of the implicit
  `wait_for_reply` budget (default `max(5 × typical_latency_seconds, 60)`).
- **`x_skip_wait_for_reply: true`** — skip the implicit `wait_for_reply`
  after submit entirely. Used by capabilities whose submit yields a CTA /
  user handoff rather than a text reply (e.g. Amap `navigate_to`), where
  polling for a reply would burn the full timeout on a non-text surface.
- **`x_pre_invocation_steps: [<step>, ...]`** — extra steps run AFTER entry
  but BEFORE focusing the input field. Used to lock the chat surface into
  a sub-mode (e.g. WPS "AI PPT").
- **`x_post_result_flow: { steps: [<step>, ...] }`** — extra steps run
  AFTER `wait_for_reply` returns. Used for in-app confirmation flows
  (e.g. tongyi's `order_food` taps through the order sheet, stopping
  before payment).
- **`x_capture_full_reply: true | { max_scrolls: N }`** — after the reply
  is done, scroll the chat surface and capture each frame via VLM, then
  stitch into one text blob. Default `max_scrolls=6`. Used for stacked
  POI cards or multi-paragraph answers exceeding one viewport.

### Entry / output extensions

- **`entry.x_prepare_fresh_conversation: { description?, steps: [<step>, ...] }`** —
  steps run at the very start of each task (after `open_app`) to clear
  prior conversation context. Can be disabled per-run via the
  `RELAY_FRESH_CONV=0` env var.
- **`output.method: copy_button`** (extends §7.1 enum) +
  **`output.x_copy_button: { text?, screen_fraction?, valid_x?, valid_y? }`** —
  after the reply lands, tap the in-app 复制 button so the answer ends up
  on the device clipboard. Locator priority: VLM grounding via `text`,
  then `screen_fraction`, with `valid_x` / `valid_y` ranges as a sanity
  filter against VLM mis-grounding.

## 14. Relationship to A2A and MCP

This spec deliberately reuses concepts from Google's **A2A AgentCard** (`name`, `description`, `capabilities`, `skills`) and Anthropic's **MCP** tool descriptors (rich natural-language descriptions, structured side-effect metadata). The intent is forward compatibility: a future `to_a2a()` projection should be lossless for the subset of fields A2A expresses.

Differences are deliberate:

- RelayAgent is **GUI-mediated by default**, not RPC-mediated. The `entry` and `invocation` blocks have no A2A analogue and are this spec's primary contribution.
- RelayAgent forces explicit `executable` and `handoff_to_user_required` flags because most in-app agents today are partially capable, and routing mistakes have user-visible cost.

## 15. Open questions

Tracked in `SPEC-OPEN-QUESTIONS.md`. Highlights:

- Should `entry` regain ordered fallback paths (and OCR-based selectors) for apps that ship without accessibility ids? (Dropped in v0.1 — no card used it.)
- How to express agents that are gated behind A/B experiments or staged rollouts?
- Should `capabilities` allow structured parameter schemas (à la JSON Schema) in addition to natural-language `description` + `example_prompts`?

---

*End of SPEC v0.1.*
