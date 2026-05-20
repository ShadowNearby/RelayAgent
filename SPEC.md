# AppAgentCards Specification

**Version:** 0.1 (draft)
**Status:** Working draft — breaking changes expected before 1.0
**License:** Apache-2.0

---

## 1. Motivation

Mobile OS-level agents (HarmonyOS Xiaoyi, Apple Intelligence, etc.) increasingly need to delegate user intents into third-party apps. Two existing paths each have problems:

- **A2A / App Intents / HMAF** — require explicit cooperation from app vendors; super-apps largely opt out or expose only marginal capabilities.
- **Pure GUI agents** — operate at the app's full UI surface, are brittle to UI changes, slow, and legally gray.

This spec defines a third path: a **machine-readable card** that describes how an OS-level agent can hand off a natural-language task to an explicitly selected app's **own embedded agent** (the AI tab inside Amap, Yuanbao inside WeChat, Xiaohongshu's AI search, etc.) via a minimal, GUI-mediated entry — a deep link when one exists, otherwise a short tap sequence to reach the in-app agent's input field.

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
- Top-level keys are fixed; unknown top-level keys MUST be rejected by validators.
- Extension keys at any nested level MUST be prefixed with `x_` and are ignored by reference SDKs.

## 4. Top-level schema

```yaml
spec_version: "0.1"          # required, string, semver of this SPEC
card_version: "1.3.0"        # required, semver of this card file
app_id: "com.autonavi.minimap" # required, package name (Android) or bundle id (iOS)
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
  type: native_in_app_agent          # required, see §5.1
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
  output:                            # optional, see §7.2
    ...
```

### 5.1 `type` enum

| Value                | Meaning |
| -------------------- | ------- |
| `native_in_app_agent`| Conversational agent with its own UI surface inside the app (e.g. Tongyi Qianwen's main chat interface). |
| `chat_widget`        | Floating/embedded chat panel surfaced from another feature (e.g. Yuanbao in WeChat). |
| `smart_search`       | AI-augmented search box that accepts natural language (e.g. Taobao's shopping assistant). |
| `voice_assistant`    | Voice-first in-app assistant where text input is unavailable or secondary. |

The OS-level router MAY use `type` to choose interaction style (e.g. `voice_assistant` may need TTS injection rather than text).

## 6. `entry` block

Describes how to reach the input surface of the embedded agent from a cold app launch.

```yaml
entry:
  primary:
    method: tap_sequence           # deep_link | intent | tap_sequence
    steps:
      - tap: { text: "Press to Talk" }  # bottom-center tab; no deep link exposed
  fallback: []                     # optional, ordered list, tried in order
```

A `deep_link` primary is preferred when one exists and is stable, but most super-apps do not expose a public scheme that reaches an internal AI surface — `tap_sequence` is the realistic default.

### 6.1 `method` enum

- **`deep_link`** — `uri` is opened via the platform's URL handler. Preferred when stable.
- **`intent`** — Android explicit intent or iOS App Intent. Required fields: `action`, optional `component`, `extras`.
- **`tap_sequence`** — ordered list of `steps`, each one of:
  - `tap: { accessibility_id | resource_id | text | text_contains | xpath }`
  - `tap_label: { text | text_contains | text_or_desc | text_or_desc_contains | accessibility_id }` — searches the visible screen for a label, optionally scrolling to find it, then taps. Optional fields: `timeout_seconds` (float), `scroll_attempts` (int), `required` (bool).
  - `tap_screen_fraction: { x_ratio, y_ratio }` — taps at a fractional screen position. Optional `label` (string) for logging.
  - `swipe: { from, to, duration_ms }`
  - `wait: { ms }` or `wait: { until: <selector> }`

Selectors MUST prefer, in order: `accessibility_id` > `resource_id` > `text` > `text_contains` > `xpath`. `xpath` is allowed but discouraged — fragile across versions.

In v0.1 a selector is a **single field**. Real apps often share one `resource_id` across sibling nodes (e.g. all bottom tabs in a tab bar), forcing authors to fall back to `text`. Composite selectors (`{ resource_id, text }`) are tracked in OQ-10.

**Bounds as a last-resort selector (`x_bounds`).** When an element has neither a usable `resource_id`, `accessibility_id`, nor stable `text`, the author MAY fall back to absolute-bounds:

```yaml
trigger:
  x_bounds:
    box: [x1, y1, x2, y2]          # required, integer pixels on the verified device
    anchor: bottom_right            # optional: top_left | top_right | bottom_left | bottom_right | center | none
```

`box` is the pixel rectangle of the target node as observed on the verified device. `anchor`, when present, is the author's claim about how the element is pinned in the layout — used by routers to choose a remapping strategy across devices. Omit `anchor` when uncertain; routers will fall back to linear bi-axial scaling.

Whenever **any** `x_bounds` appears in a card, the `provenance` block MUST include `x_device_metrics` recording the resolution and density the box was measured against (see §9). Routers without device-metrics-aware remapping logic SHOULD refuse `x_bounds`-only cards on a device whose resolution differs from the recorded reference.

Full promotion of `x_bounds` to a first-class `bounds` selector with richer anchor / dp / OCR options is deferred to v0.2 (OQ-11).

**Tap-through on non-clickable anchors.** Selecting a non-clickable node (e.g. a TextView label, a placeholder hint) is explicitly permitted. Routers perform the tap at the selected node's bounds; touch dispatch propagates the event to the nearest clickable ancestor. Card authors SHOULD prefer the most stable visible text anchor over `x_bounds` whenever the parent clickable region intercepts the touch.

### 6.2 Preconditions

```yaml
entry:
  preconditions:                  # optional
    - type: login_required
    - type: permission
      permission: location.precise
    - type: first_run_dialog
      dismiss: { tap: { text: "Allow" } }
```

The OS router is expected to surface unmet preconditions to the user, not silently bypass them.

## 7. `invocation` block

```yaml
invocation:
  input:
    # EditText with no resource-id; placeholder/hint exposed as `text` while
    # unfocused. Selector is valid only before the user starts typing — so
    # routers MUST tap the field first, then submit text, in that order.
    field: { text: "Ask me anything..." }
    max_chars: 500                 # optional
  submit:
    # Same ViewGroup acts as mic (empty input) or send (non-empty input).
    # No id / no content-desc — absolute bounds recorded as last resort.
    trigger:
      x_bounds:
        box: [946, 2075, 1009, 2138]
        anchor: bottom_right
  prompt_template: "{{user_prompt}}"   # optional, default is identity
```

The OS agent SHOULD pass the **user's original phrasing** through `prompt_template` whenever possible. Rewriting the user's prompt before handoff defeats the design — the in-app agent is presumed better at interpreting requests in its own domain.

### 7.1 `prompt_template` variables

- `{{user_prompt}}` — raw user utterance.
- `{{user_locale}}` — BCP-47.
- `{{capability_id}}` — id of the capability the router selected (see §8); useful when one agent serves many capabilities and benefits from a hint.

### 7.2 `output` block (optional)

```yaml
output:
  method: none                       # none | screen_text_extract | accessibility_tree
  completion_signal:
    type: text_match
    patterns: ["Order placed", "Payment page"]
    timeout_ms: 30000
```

`method: none` is the recommended default for v0.1: the OS agent hands off and stops. Reading results back is explicitly out of scope for this version (see §13).

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
    side_effects:                     # required, see §8.2
      - payment
    requires_login: true              # required
    reversible: false                 # required
    handoff_to_user_required: true    # required, see §8.3
    typical_latency_seconds: 10       # optional
    failure_modes:                    # optional but recommended
      - "Multi-terminal airports trigger drop-off disambiguation panel"
      - "Not logged in → redirects to login page"
```

### 8.1 `executable`

- `true` — the in-app agent can complete the task end-to-end (subject to user confirmation for irreversible side effects).
- `false` — the agent only **suggests / surfaces / informs**. The OS router MUST NOT promise the user that the action will be done.

This distinction matters: many in-app "AI assistants" narrow choices but never close the loop. Mislabeling them breaks user trust at the OS level.

### 8.2 `side_effects` enum

Zero or more of:

| Value                    | Meaning |
| ------------------------ | ------- |
| `payment`                | Spends money or commits a financial obligation. |
| `external_communication` | Sends a message, post, or comment to another party. |
| `data_write`             | Modifies user-owned records (orders, drafts, files). |
| `data_delete`            | Deletes user-owned records. |
| `physical_action`        | Triggers an offline action (smart-home, vehicle, etc.). |
| `none`                   | Pure read / recommendation. MUST be the only entry if present. |

### 8.3 `handoff_to_user_required`

If `true`, the OS router MUST return foreground control to the user before the capability's terminal action and MUST NOT auto-tap, auto-confirm, or otherwise complete the action on the user's behalf.

Two distinct reasons to set this `true`:

1. **Safety** — the action is irreversible or has user-visible cost (payment, message send, delete). Auto-confirming is unsafe.
2. **Author intent** — the in-app agent presents a one-tap CTA (e.g. a "Start Navigation" button) where the user retains meaningful choice (mode, route, target). Auto-tapping pre-empts a choice the user expects to make.

Either reason is sufficient. Card authors are encouraged to explain *why* `true` in the capability's `description`.

This field is **redundant-but-required** with `side_effects` to force authors to make the safety decision explicitly. Validators MUST warn (not error) when `payment`, `data_delete`, or `external_communication` ∈ `side_effects` and `handoff_to_user_required: false`.

## 9. `provenance` block

```yaml
provenance:
  last_verified: "2026-05-10"        # required, ISO 8601 date
  verified_app_version: "12.4.1"     # required
  verified_os: "android-14"          # required: "android-NN" | "ios-NN" | "harmonyos-N.N"
  verified_device: "Pixel 8"         # optional
  verification_method: manual        # required: manual | scripted | community_reported
  evidence_url: ""                   # optional, link to script / video / screenshots
  x_device_metrics:                  # required when card uses any `x_bounds` selector
    resolution_px: [1080, 2424]
    density_dpi: 420
```

A card is considered **stale** by tooling if `last_verified` is more than 90 days old, or if `verified_app_version` is more than two minor versions behind the current store version. Stale cards MUST still be served by the registry, marked as stale, and SHOULD NOT be used by routers without an explicit override.

`x_device_metrics` is mandatory when any `x_bounds` selector is present, so routers can remap pixel coordinates onto the target device. v0.1 ships this under the `x_` prefix; v0.2 will promote it (OQ-11).

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

- `spec_version` follows this document. Breaking changes bump major.
- `card_version` is per-card semver. Bump **major** when capability ids are removed or renamed; **minor** when capabilities or fields are added; **patch** for prose, examples, or `provenance` updates.
- Routers MUST refuse cards whose `spec_version` major exceeds the version they implement.

## 12. Conformance

A **conforming card** MUST:

1. Validate against the schema (JSON Schema mirror at `spec/schema.json`, normative).
2. Have at least one capability with at least two `example_prompts`.
3. Have `provenance.last_verified` set to a real, dated verification.
4. For every capability with `payment`, `data_delete`, or `external_communication` in `side_effects`, set `handoff_to_user_required: true`.

A **conforming router** MUST:

1. Pass user prompts to the embedded agent without semantic rewriting beyond `prompt_template` substitution.
2. Honor `handoff_to_user_required`.
3. Surface unmet `entry.preconditions` to the user before invocation.
4. Refuse to use cards marked stale unless the user explicitly opts in.

## 13. Out of scope for v0.1

- **Result read-back** from the in-app agent. Routers hand off and stop; cross-app aggregation (e.g. price comparison) is deferred.
- **Multi-turn sessions** with the embedded agent. v0.1 is single-shot.
- **Authentication delegation** between OS agent and in-app agent. The user's existing in-app login is the trust anchor.
- **Discovery protocol.** v0.1 ships as a static GitHub registry; a network discovery mechanism (à la `.well-known/agent-card.json`) is a candidate for v0.2.

## 14. Relationship to A2A and MCP

This spec deliberately reuses concepts from Google's **A2A AgentCard** (`name`, `description`, `capabilities`, `skills`) and Anthropic's **MCP** tool descriptors (rich natural-language descriptions, structured side-effect metadata). The intent is forward compatibility: a future `to_a2a()` projection should be lossless for the subset of fields A2A expresses.

Differences are deliberate:

- AppAgentCards is **GUI-mediated by default**, not RPC-mediated. The `entry` and `invocation` blocks have no A2A analogue and are this spec's primary contribution.
- AppAgentCards forces explicit `executable` and `handoff_to_user_required` flags because most in-app agents today are partially capable, and routing mistakes have user-visible cost.

## 15. Open questions

Tracked in `SPEC-OPEN-QUESTIONS.md`. Highlights:

- Should `entry.fallback` support OCR-based selectors for apps that ship without accessibility ids?
- How to express agents that are gated behind A/B experiments or staged rollouts?
- Should `capabilities` allow structured parameter schemas (à la JSON Schema) in addition to natural-language `description` + `example_prompts`?

---

*End of SPEC v0.1.*
