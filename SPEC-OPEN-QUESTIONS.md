# Open Questions

Design questions deferred from SPEC v0.1. Each will either get resolved into a SPEC change or explicitly closed as out-of-scope. Discuss in issues; reference the question id (e.g. `OQ-3`) in PRs.

---

## OQ-1 — Structured parameter schemas for capabilities

**Status:** open. **Likely target:** v0.2.

Today a capability describes itself with `description` + `example_prompts` and trusts an LLM router to extract parameters from the user's utterance. Should we additionally allow a JSON Schema for parameters, a la MCP tools?

- **For:** smaller models route more reliably; non-LLM routers become possible; clearer contract.
- **Against:** most in-app agents accept a free-form prompt anyway — schemas would describe the *router's* parsing, not the *agent's* interface, which is misleading. Also raises the bar for card authors.

Decision criterion: does writing the first 5 reference manifests feel like it's missing this? Defer until then.

## OQ-2 — OCR / vision selectors

**Status:** open. **Likely target:** v0.2.

Many apps ship without accessibility ids on AI-related buttons (sometimes deliberately). `tap_sequence` currently allows `text` / `text_contains` / `xpath`, all of which break on dynamic content. Should `entry.fallback` support a vision-based selector (`tap: { match_image: "..." }` or `tap: { match_text_ocr: "..." }`)?

- **For:** sometimes the only path that works.
- **Against:** vision matching is non-deterministic, hard to validate in CI, and cards become unreviewable.

If we add this, it must be marked clearly as a last-resort method, and cards relying on it should be flagged in registry tooling.

## OQ-3 — A/B experiments and staged rollouts

**Status:** open.

A capability may exist for some users and not others (regional rollout, A/B test, staged release). Today `constraints.region` and `constraints.app_version_min` are the only knobs. Should we add:

- `constraints.rollout_probability: 0.5` (best-effort signal that the capability is staged)?
- A way to express "available only if the user has feature flag X"?

Probably not — we can't observe these from outside, and false confidence is worse than no signal. But worth documenting why we're not adding it.

## OQ-4 — Multi-turn handoff

**Status:** open. **Likely target:** v0.3+.

v0.1 is single-shot: one user prompt, one handoff, no return. Real conversations with in-app agents are multi-turn. How should the OS router model this?

- Option A: don't — once handed off, the user just continues talking inside the app.
- Option B: define a `session` block describing how to keep / resume / inject into an existing chat.

A is the v0.1 answer. B may matter once OS agents start orchestrating multi-app workflows.

## OQ-5 — Result read-back

**Status:** open. **Likely target:** v0.3+.

Cross-app aggregation (e.g. "compare prices on JD.com and Taobao") requires reading the in-app agent's response back into the OS router. SPEC §13 explicitly defers this. When we add it, the question is whether to standardize:

- screen-text scraping with `output.completion_signal` (already partially defined),
- accessibility-tree extraction,
- or some richer convention requiring app cooperation.

Tied to OQ-1 — read-back without a structured response schema is mostly LLM summarization of screenshots.

## OQ-6 — Discovery / distribution

**Status:** open. **Likely target:** v0.2.

v0.1 ships as a static GitHub registry. Realistically, a phone OEM shipping this needs:

- A signed, versioned distribution channel.
- A way for OEMs to override / disable specific cards locally.
- A `.well-known/agent-card.json`-style endpoint so app vendors can self-publish.

The third option is the long-term right answer, but we can't force vendors. A community registry with a clear migration path is the v0.1 stance.

## OQ-7 — Trust and signing

**Status:** open. **Likely target:** v0.2.

A malicious card could route a user's "transfer 100 to mom" to the wrong app. Mitigations to consider:

- Card signing by maintainers.
- Required app-package-signature pinning in cards (`app_signing_cert_sha256`).
- Router-side capability allowlists.

Probably ship signing in v0.2, package-signature pinning when we have a real attack to point at.

## OQ-8 — Internationalization beyond zh-CN

**Status:** open.

The MVP target apps are all China-market. The SPEC allows `locale: ["zh-CN", "en-US", ...]` but no MVP card will exercise multi-locale. When (Apple Intelligence + Siri) cards arrive, we'll find out what's missing.

## OQ-9 — How to keep cards fresh

**Status:** open, important.

Apps update monthly. Manual `provenance` refresh by humans does not scale. Options:

- CI bot that opens "card may be stale" issues based on store version diffs.
- Optional automated runners per card, executed nightly on a device farm (who pays?).
- Crowdsourced "this card worked / didn't work for me" feedback channel.

Some combination of all three. The 90-day stale rule in SPEC §9 is a placeholder until we have real signal.

## OQ-10 — Composite selectors

**Status:** open. **Likely target:** v0.2. **Surfaced by:** `com.autonavi.minimap.yaml`.

SPEC §6.1 defines selectors as a single field from `{ accessibility_id, resource_id, text, text_contains, xpath }`. Real apps often expose **only generic ids shared across siblings** — e.g. all five Amap bottom tabs share `resource-id="com.autonavi.minimap:id/tab_name_v2"`, distinguished only by `text`. A single-field selector either misses uniqueness (`resource_id` alone matches 5 nodes) or is fragile (`text` alone breaks when copy changes).

Proposal: allow a step's selector to be an AND of multiple keys:

```yaml
- tap: { resource_id: "com.autonavi.minimap:id/tab_name_v2", text: "Press to Talk" }
```

Decision criterion: does the second reference card hit this again? If yes, ship in v0.2.

## OQ-11 — Bounds / spatial selectors as last resort

**Status:** partially resolved in v0.1. **Likely full target:** v0.2. **Surfaced by:** `com.autonavi.minimap.yaml`, expanded by all four reference cards.

Some critical elements are completely unidentifiable through the accessibility tree: no resource-id, no content-desc, no text. The Amap send / microphone button is one example — a `ViewGroup` whose only stable property is its on-screen rectangle.

**v0.1 partial fix shipped (see SPEC §6.1 + §9):**

- `x_bounds` is now a structured object: `{ box: [x1,y1,x2,y2], anchor?: <enum> }`.
- `provenance.x_device_metrics` (resolution + density_dpi) is mandatory whenever any `x_bounds` is used, so routers can perform device-aware remapping.
- All four reference manifests have been retrofitted.

**Still deferred to v0.2:**

- Promote `x_bounds` to a first-class `bounds` selector (drop the `x_` prefix and "extension key" framing).
- Relative selectors (`right_of: <other-selector>`, `inside: <other-selector>`) for elements pinned relative to a stable neighbor — much more robust than absolute bounds when the neighbor is identifiable.
- Optional `dp_offset` / `dp_size` describing a node by its anchor-relative dp position rather than absolute pixels — matches how Android UIs are actually laid out and is robust across both resolution and density.
- OCR / vision selectors as a final floor for apps that disable the accessibility tree entirely (see OQ-15) — overlaps with OQ-2.

## OQ-12 — `executable` granularity and partner-auth tiers

**Status:** open, important. **Likely target:** v0.2. **Surfaced by:** `com.aliyun.tongyi.yaml`.

SPEC §8.1 defines `executable` as a boolean: "true = can complete the task end-to-end". This breaks down when the agent is an **orchestrator** that delegates to partner services, because the user-visible friction varies wildly even when the boolean is identically `true`.

Empirical from Tongyi Qianwen v6.9.1 — three distinct auth tiers all classified `executable: true`:

| Tier | Example | User-visible friction |
| --- | --- | --- |
| `oauth_preauthorized` | Qianwen → Fliggy / Amap / Taobao (pre-authorized in agent's auth management page) | None — invisible delegation |
| `oauth_bind_required` | Qianwen → Taobao Flash / Damai (first invocation triggers OAuth modal) | Medium — one-time modal |
| `service_credential` | Qianwen → Fliggy → 12306 (Fliggy OAuth passes, but 12306 still demands its own password each session) | High — credentials per session, even with App-level OAuth in place |

A single boolean conflates all three, so OS routers can't promise the user the right thing. ("I'll book that train" is a lie at the `service_credential` tier.)

Proposal directions for v0.2:

- Replace boolean with enum: `complete_natively | complete_via_partner_oauth | complete_via_external_credential | recommend_with_cta | recommend_only`.
- Or keep boolean and add `partner_auth: { kind, partner_app_id?, partner_service? }` as a separate field. More backwards-compatible.
- Either way, surface the friction so the OS router's "I will do X" message matches reality.

Decision criterion: how does the third reference manifest feel? If most of its capabilities are agent-orchestrated rather than self-contained (which they will be for any AI-first app), this needs to ship in v0.2.

## OQ-15 — Apps that disable the accessibility tree

**Status:** open. **Likely target:** v0.2+ (depends on OCR/vision tooling). **Surfaced by:** `com.tencent.mm` (WeChat), expected to repeat for Douyin, Pinduoduo, and most banking / brokerage apps.

When we attempted a WeChat reference card, `uiautomator dump` returned an empty hierarchy (one root node with `bounds="[0,0][0,0]"`, ~400 bytes total) on a normal production install. The accessibility tree is intentionally suppressed — this is industry-standard anti-automation across the super-app tier.

Consequences for the SPEC:

- **Every** v0.1 selector — `accessibility_id`, `resource_id`, `text`, `text_contains`, `xpath` — is unusable, because all five require the a11y tree.
- The `x_bounds` last-resort (OQ-11) is unusable too at the **authoring** stage: without a tree, the author can't discover the bounds to write down.
- v0.1 cards for such apps would be effectively ineligible per the CONTRIBUTING quality bar ("verify on a real device, walk every entry path").

Options for v0.2:

- **Vision / OCR selectors** ("tap the text 'AI Search' as detected by OCR within region X"). Reuses OQ-2 work, requires the router to ship an OCR model. Highest reach, hardest to standardize.
- **Visual template matching** (small reference PNG of the button) — works for stable icons, breaks on dynamic content.
- **Explicit "a11y-blocked" SPEC carve-out**: cards for these apps live in a separate `manifests/a11y-blocked/` directory, use a distinct sub-schema, and are flagged as low-confidence by the registry. Honest about the limit rather than pretending to support these apps with normal cards.

For v0.1 we explicitly document this category as out of scope. WeChat will not have a reference card until v0.2 ships at least one of the above mechanisms.
