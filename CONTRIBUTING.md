# Contributing

This project lives or dies on **card quality**, not card count — please read this before opening a PR.

## What we accept

- **New cards** for apps not yet covered.
- **Card updates** — `provenance` refresh, new capabilities, fixed selectors after an app update.
- **New flows** — multi-app YAMLs under `manifests/_flows/` composing existing cards (see the two reference flows for the format).
- **SPEC changes** — open an issue first; SPEC changes require a version bump and rationale.

## What we do not accept (yet)

- Cards for apps without a real, user-visible embedded agent. ("This app has a search box that calls an LLM" is not enough — see SPEC §5.1 `type`.)
- Cards built from reverse-engineered private endpoints. Cards describe **GUI-mediated handoff**, not bypasses around it.
- Bulk-generated cards without manual verification on a real device.

## Submitting a new card

1. **Read [SPEC.md](SPEC.md).** Especially §8 (`executable`, `side_effects`, `handoff_to_user_required`) — getting these wrong has user-visible cost.
2. **Copy a reference card** from `manifests/` as a starting template.
3. **Verify on a real device.** Open the app, walk every `entry` path, send every `example_prompt` for every capability, watch what happens. Record:
   - App version (`provenance.verified_app_version`)
   - OS version (`provenance.verified_os`)
   - Date (`provenance.last_verified`)
4. **Open a PR** with:
   - The card file under `manifests/<reverse-dns-app-id>.yaml`.
   - A short note in the PR description: device + app version used to verify, anything you couldn't get to work, anything fragile.

## Card review checklist

Reviewers will look for:

- [ ] `spec_version` matches current SPEC.
- [ ] All required fields present, no unknown top-level keys.
- [ ] Every `capability` has ≥2 real example prompts (not paraphrased from the description).
- [ ] `executable` honestly reflects whether the agent closes the loop or only suggests.
- [ ] `side_effects` is complete — does this capability spend money? send messages? delete things?
- [ ] `handoff_to_user_required: true` for any capability with `payment`, `data_delete`, or `external_communication` in `side_effects`.
- [ ] `entry.primary` prefers `deep_link` or `intent` over `tap_sequence` when one exists.
- [ ] Selectors prefer `accessibility_id` / `resource_id` over `text` / `xpath`.
- [ ] If **any** `x_bounds` selector is used, `provenance.x_device_metrics` is present with `resolution_px` and `density_dpi` of the verified device. (Use `adb shell wm size` and `adb shell wm density` to get the values.)
- [ ] `x_bounds` uses the structured form `{ box: [x1,y1,x2,y2], anchor?: ... }` — bare strings like `"[x1,y1][x2,y2]"` are rejected by the schema.
- [ ] `provenance.last_verified` is within 30 days.
- [ ] `known_issues` calls out anything that bit you during verification.

## Updating a stale card

Cards go stale fast — apps update monthly. If you bump a card:

- Bump `card_version` per SPEC §11 (patch for prose / provenance, minor for new capabilities, major for removed/renamed capability ids).
- Update the entire `provenance` block; don't just change the date.
- If selectors changed because of a UI redesign, leave the old `tap_sequence` in the PR description so reviewers can confirm the change was real.

## SPEC change proposals

1. Open an issue tagged `spec` describing the problem and at least one proposed change.
2. Wait for discussion before opening a PR — SPEC changes ripple through every card.
3. SPEC changes MUST bump `spec_version` (patch for clarification, minor for additive, major for breaking).
4. Breaking SPEC changes require a migration note in the PR.

## Code of conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Legal

- All contributions are licensed under Apache-2.0 (see [LICENSE](LICENSE)).
- By submitting, you confirm you have the right to contribute the content.
- Do not submit information obtained by violating an app's terms of service (decompilation, scraping private APIs, etc.). Cards describe what a user can do by hand; that's the line.
