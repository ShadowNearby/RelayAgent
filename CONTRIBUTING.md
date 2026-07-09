# Contributing

This project lives or dies on **card quality**, not card count.

## What we accept

- **New cards** for apps with a real, user-visible embedded agent.
- **Card updates** — `provenance` refresh, new capabilities, fixed selectors after an app update.
- **SPEC changes** — open a `spec`-tagged issue first; they ripple through every card and require a `spec_version` bump.

We do **not** accept cards for apps without a genuine embedded agent (a search box that calls an LLM is not enough — SPEC §5), cards built from reverse-engineered private endpoints, or bulk-generated cards not verified on a real device.

## Submitting a card

1. Read [SPEC.md](SPEC.md) — especially §8 (`executable`, `handoff_to_user_required`); getting these wrong has user-visible cost.
2. Copy a reference card from `manifests/` as a template.
3. **Verify on a real device**: open the app, walk every `entry` path, send every `example_prompt`. Record the app version, OS version, and date into `provenance`.
4. Validate locally (CI runs the same check):
   ```bash
   uv run python scripts/validate/validate_manifests.py manifests/<your-card>.yaml
   ```
5. Open a PR with the card at `manifests/<reverse-dns-app-id>.yaml` and a note: device + app version used, anything fragile or that didn't work.

## Review checklist

- [ ] `validate_manifests.py` passes; `spec_version` matches current SPEC; no unknown top-level keys.
- [ ] Every capability has ≥2 real example prompts (not paraphrased from the description).
- [ ] `executable` honestly reflects whether the agent closes the loop or only suggests.
- [ ] `handoff_to_user_required: true` for any capability whose completion is irreversible — spends money, sends messages, deletes data, confirms a ride/booking/order (SPEC §8.2).
- [ ] Selectors prefer `accessibility_id` > `resource_id` > `text` > `text_contains`; fall back to `screen_fraction: { x_ratio, y_ratio }` (in `[0,1]`, at the affordance's visible center) only when nothing else is exposed (SPEC §6.1).
- [ ] `provenance.last_verified` within 30 days; `known_issues` calls out anything that bit you.

## Updating a stale card

Bump `card_version` per SPEC §11 (patch for prose/provenance, minor for new capabilities or re-pathed selectors, major for removed/renamed ids), and refresh the **entire** `provenance` block. If selectors changed for a UI redesign, leave the old `tap_sequence` in the PR description.

## Legal

Contributions are licensed under Apache-2.0 ([LICENSE](LICENSE)); by submitting you confirm you have the right to contribute the content. Do not submit anything obtained by violating an app's terms of service (decompilation, scraping private APIs). Cards describe what a user can do by hand — that's the line.
