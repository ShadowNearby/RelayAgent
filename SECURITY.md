# Security Policy

## Reporting

Report vulnerabilities privately via [GitHub Security Advisories](https://github.com/ShadowNearby/RelayAgent/security/advisories/new) — **not** a public issue. Include repro steps, the affected component (host runtime / Android app / a specific card), and the impact. Expect a first response within a week.

## In scope

RelayAgent drives real, logged-in apps on a real device, so beyond classic code-execution bugs we especially care about:

- **Handoff-contract bypasses** — any path where a `handoff_to_user_required` capability reaches an irreversible action (payment, order, ride) without returning control to the user.
- **Prompt / manifest injection** — a crafted card, cached plan, or in-app reply that makes the runtime act outside the declared capability.
- **Credential leakage** — `.env` / LLM keys or Android config leaking into trajectory logs, `steps/` screenshots, or crash output.

## Out of scope

- Bugs in the third-party apps the cards describe (report to the vendor).
- In-app agents' own model behavior (wrong or hallucinated answers).
- Attacks that require a device already adb-authorized to the attacker — adb access is the trust boundary by design.
