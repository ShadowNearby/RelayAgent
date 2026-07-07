---
name: New card proposal
about: Propose an app whose embedded agent should get a card
labels: card, new-card
---

**App**: name + package id (reverse-DNS)

**Where its embedded agent lives**

How a user reaches it by hand (tab / button path), and what it can do once
you're there. Screenshots welcome.

**Why it qualifies**

Per CONTRIBUTING.md we only accept apps with a real, user-visible embedded
agent — not just an LLM-backed search box (SPEC §5 `embedded_agent`).

**Capabilities you'd declare**

Rough list; mark any that would need `handoff_to_user_required: true`
(payment, ordering, booking — anything irreversible).

**Can you verify on a real device?** yes / no
