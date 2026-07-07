# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately via
[GitHub Security Advisories](https://github.com/ShadowNearby/RelayAgent/security/advisories/new)
— do **not** open a public issue for anything exploitable.

You should get an initial response within a week. Please include reproduction
steps, the affected component (host runtime / Android app / a specific card),
and the impact you see.

## What counts as a vulnerability here

RelayAgent drives real, logged-in apps on a real device, so beyond classic
code-execution bugs we especially care about:

- **Handoff-contract bypasses** — any path where a capability marked
  `handoff_to_user_required: true` can reach an irreversible action (payment,
  order submission, ride confirmation) without an `ask_user` returning control
  to the human.
- **Prompt/manifest injection** — a crafted card, cached plan, or in-app agent
  reply that makes the runtime execute actions outside the declared
  capability.
- **Credential leakage** — `.env` / LLM-key material or Android
  `SharedPreferences` config ending up in trajectory logs, `steps/`
  screenshots metadata, or crash output.

## Out of scope

- Vulnerabilities in the third-party apps the cards describe (report those to
  the vendor).
- The in-app agents' own model behavior (hallucinated replies, wrong answers).
- Attacks requiring a device that is already adb-authorized to the attacker —
  adb access is this tool's trust boundary by design.
