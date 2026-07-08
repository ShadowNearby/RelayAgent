"""Runtime failure-recovery ladder for flow legs (roadmap P1).

Today a failed app leg kills the whole flow (hard failures raise in
`flow_runner._run_app_step`) or silently ships a wrong answer downstream
(judge failures only log). This module gives `FlowRunner` a bounded recovery
ladder to climb before giving up:

    retry (same app/capability, fresh conversation, optional reword)
      → reroute (three-stage router with the failed pair excluded)
        → MobileWorld fallback leg (manifest-free general_e2e)
          → partial-success report

Failure taxonomy (`LegFailure.kind`) decides the entry tier:

- ``env_fail``   — device/IME/adb layer died before the run loop (subprocess
  rc != 0 with no summary written). Never recovered: the next attempt hits the
  same wall; surface the device problem instead.
- ``route_fail`` — the leg finished but the judge says it landed in the wrong
  feature / answered off-goal (`failure_kind == wrong_feature`). Entry: retry
  with a reworded prompt, then reroute.
- ``app_fail``   — the right feature didn't deliver: no reply captured when one
  was needed, a non-terminal exit, or the judge saw an app-side error wall
  (`failure_kind == app_error`). Entry: plain retry, then reroute.

Safety red line: a capability with `handoff_to_user_required: true` gets the
retry tier ONLY. Never reroute it (a different app would redo user-visible
preparation) and NEVER hand it to MobileWorld — general_e2e drives the GUI
freely with no handoff contract, which is exactly how a recovery could cross
an irreversible action.

Budget guardrails (env, all bounded so recovery can't run away):

- ``RELAY_RECOVERY`` (default ``1``) — ``0`` disables the ladder entirely and
  restores the old fail-fast behavior (benchmarks force this off; see
  run_benchmark_test.py).
- ``RELAY_RECOVERY_MAX_RETRIES`` (default ``1``) — same-pair retries per leg.
- ``RELAY_RECOVERY_MAX_LEGS`` (default ``2``) — extra leg executions per flow,
  across all tiers.
- ``RELAY_RECOVERY_TOKEN_BUDGET`` (default ``15000``) — total tokens the extra
  legs may consume (read off each attempt's summary `token_usage`); the ladder
  stops climbing once exceeded.

Every attempt is recorded (`recovery.json` next to the original leg's
trajectory + the flow-level `flow_report.json`) so R4's evaluation can price
the ladder tier by tier.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from agents.flow.leg_judge import FAILURE, WRONG_FEATURE, LegVerdict
from agents.routing.capability_matrix_router import (
    FoundationNotApplicable,
    NoRunnableAppForCapability,
    load_matrix,
    route,
)
from agents.routing.card_catalog import build_catalog

# Flow-level failure taxonomy (roadmap P1-R0).
ENV_FAIL = "env_fail"
ROUTE_FAIL = "route_fail"
APP_FAIL = "app_fail"

# Ladder tiers, in escalation order.
TIER_RETRY = "retry"
TIER_REROUTE = "reroute"
TIER_MW = "mw_fallback"


@dataclass(frozen=True)
class LegFailure:
    """One classified leg failure.

    `fatal` mirrors today's behavior split: a hard failure (would have raised)
    stops the flow when recovery is off or exhausted; a judge-only failure
    (would have only warned) lets the flow continue with the best attempt.
    """

    kind: str
    reason: str
    fatal: bool


def classify_leg_failure(
    rc: int,
    summary: dict[str, Any],
    reply: str,
    needs_reply: bool,
    hard_error: str | None,
    verdict: LegVerdict | None,
) -> LegFailure | None:
    """Hard signals + judge verdict → one LegFailure, or None when the leg is
    good enough to commit (mirrors today's accept conditions exactly)."""
    if rc != 0 and not summary:
        # The subprocess died before the run loop ever wrote a summary — an
        # environment-layer death (backend init, IME, missing app), not
        # something another attempt can fix.
        return LegFailure(ENV_FAIL, f"leg subprocess rc={rc} with no summary written", fatal=True)
    if needs_reply and not reply:
        return LegFailure(APP_FAIL, hard_error or "no reply captured", fatal=True)
    if hard_error:
        return LegFailure(APP_FAIL, hard_error, fatal=True)
    if verdict is not None and verdict.status == FAILURE:
        kind = ROUTE_FAIL if verdict.failure_kind == WRONG_FEATURE else APP_FAIL
        return LegFailure(kind, verdict.reason, fatal=False)
    return None


_REWORD_SYSTEM = (
    "A phone assistant was given a task prompt but failed. Rewrite the prompt "
    "for a second attempt at the SAME in-app assistant: keep the goal, all "
    "proper nouns, quantities and constraints EXACTLY as they are, but phrase "
    "it more directly and unambiguously so the assistant's intent routing "
    "cannot miss. Keep the original language. Reply with ONLY the rewritten "
    "prompt text — no quotes, no explanation."
)


@dataclass
class RecoveryController:
    """Per-flow recovery state: budgets, catalog access, attempt log."""

    llm: Any
    model: str

    enabled: bool = field(default=False, init=False)
    max_retries: int = field(default=1, init=False)
    max_extra_legs: int = field(default=2, init=False)
    token_budget: int = field(default=15000, init=False)

    extra_legs_used: int = field(default=0, init=False)
    tokens_used: int = field(default=0, init=False)
    attempts: list[dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.enabled = os.getenv("RELAY_RECOVERY", "1") == "1"
        self.max_retries = int(os.getenv("RELAY_RECOVERY_MAX_RETRIES", "1"))
        self.max_extra_legs = int(os.getenv("RELAY_RECOVERY_MAX_LEGS", "2"))
        self.token_budget = int(os.getenv("RELAY_RECOVERY_TOKEN_BUDGET", "15000"))
        self._catalog: dict[str, Any] | None = None
        self._matrix: dict[str, Any] | None = None

    # ------------------------------------------------------------- budget

    def can_spend_leg(self) -> bool:
        if self.extra_legs_used >= self.max_extra_legs:
            logger.info(
                f"recovery budget exhausted: {self.extra_legs_used}/{self.max_extra_legs} extra legs"
            )
            return False
        if self.tokens_used >= self.token_budget:
            logger.info(
                f"recovery token budget exhausted: {self.tokens_used}/{self.token_budget}"
            )
            return False
        return True

    def spend_leg(self, summary: dict[str, Any] | None) -> int:
        """Account one extra leg; returns the tokens it consumed (for the
        per-attempt cost column in recovery.json — roadmap P1-R4)."""
        self.extra_legs_used += 1
        total = 0
        try:
            total = int(((summary or {}).get("token_usage") or {}).get("total_tokens") or 0)
        except (TypeError, ValueError):
            pass
        self.tokens_used += total
        return total

    # ----------------------------------------------------- capability meta

    def _ensure_catalog(self) -> None:
        if self._catalog is None:
            self._catalog = build_catalog()
            self._matrix = load_matrix()

    def _cap_meta(self, app_id: str, cap_id: str) -> dict[str, Any]:
        self._ensure_catalog()
        for app in self._catalog.get("apps", []):  # type: ignore[union-attr]
            if app.get("app_id") != app_id:
                continue
            for cap in app.get("capabilities", []):
                if cap.get("id") == cap_id:
                    return cap
        return {}

    def handoff_required(self, app_id: str, cap_id: str) -> bool:
        return bool(self._cap_meta(app_id, cap_id).get("handoff_to_user_required"))

    def has_prompt_template(self, app_id: str, cap_id: str) -> bool:
        return bool(self._cap_meta(app_id, cap_id).get("prompt_template"))

    # -------------------------------------------------------------- tiers

    def reword(self, prompt: str, failure: LegFailure, app_id: str, cap_id: str) -> str | None:
        """One cheap LLM call to rephrase the submit prompt for a retry.

        Template-driven capabilities keep their prompt verbatim (the template's
        fixed wording is the point — see prompt_template docs); returns None to
        mean "retry with the original prompt"."""
        if self.has_prompt_template(app_id, cap_id):
            logger.info(f"recovery reword skipped: {app_id}/{cap_id} uses a prompt_template")
            return None
        user = json.dumps(
            {"failed_prompt": prompt, "failure_reason": failure.reason},
            ensure_ascii=False,
        )
        if hasattr(self.llm, "purpose"):
            self.llm.purpose = "recovery_reword"
        try:
            resp = self.llm.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _REWORD_SYSTEM},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
                max_tokens=256,
            )
            out = (resp.choices[0].message.content or "").strip().strip('"').strip()
        except Exception as e:  # reword is optional — retry verbatim instead
            logger.warning(f"recovery reword failed ({e}); retrying with the original prompt")
            return None
        if not out or out == prompt:
            return None
        logger.info(f"recovery reworded prompt: {out!r}")
        return out

    def reroute(
        self, goal: str, exclude: set[tuple[str, str]]
    ) -> dict[str, Any] | None:
        """Re-run the three-stage router with the failed pair(s) excluded.

        Returns the routing decision, or None when no alternative exists (all
        candidates excluded / foundation not applicable / router error)."""
        self._ensure_catalog()
        if hasattr(self.llm, "purpose"):
            self.llm.purpose = "recovery_reroute"
        try:
            decision = route(
                goal,
                self._catalog,
                self._matrix,
                self.llm,
                self.model,
                preserve_goal=True,
                exclude=exclude,
            )
        except (NoRunnableAppForCapability, FoundationNotApplicable) as e:
            logger.info(f"recovery reroute: no alternative route ({e})")
            return None
        except Exception as e:  # router/LLM hiccup — don't let recovery crash the flow
            logger.warning(f"recovery reroute errored: {e}")
            return None
        app_id, cap_id = decision.get("app_id"), decision.get("capability_id")
        if not app_id or not cap_id or (app_id, cap_id) in exclude:
            logger.info(f"recovery reroute returned unusable pair {app_id}/{cap_id}")
            return None
        if self.has_prompt_template(app_id, cap_id):
            # Filling another capability's template needs the planner's slot
            # extraction; out of the runtime ladder's scope (v1) — skip rather
            # than send a non-conforming prompt.
            logger.info(
                f"recovery reroute skipped {app_id}/{cap_id}: target uses a prompt_template"
            )
            return None
        return decision

    # ------------------------------------------------------------- records

    def record(self, entry: dict[str, Any]) -> None:
        self.attempts.append(entry)
