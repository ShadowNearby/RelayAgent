"""LLM flow synthesizer.

Given the full catalog of apps + their embedded-agent capabilities (the
same shape `agents.routing.card_catalog.build_catalog()` produces) and a user's
natural-language request, ask the text LLM to synthesize a *new* multi-app
flow plan — the same step/bind schema that `FlowRunner` already executes,
so no new executor is needed.

Design (see CLAUDE.md + the `project_cross_app_planner` decision notes):

- Static one-shot planning: the LLM emits the whole plan once. We DON'T
  do step-by-step replanning here.
- Output reuses the flow yaml shape (`app_step` / `ask_user` / `extract` /
  `bind`); there is no `inputs` block because the request is concrete and
  literal values get baked straight into the step prompts.
- App/capability selection is resolved after step synthesis by the shared
  matrix-backed three-stage router. We then validate the plan locally (known
  ids, no dangling `{var}` reference, handoff legs followed by ask_user, unique
  binds). On a routing or validation error we feed the error list back to the
  LLM for a corrected plan (`_repair`) and re-route + re-validate, up to
  `_REPAIR_ROUNDS` rounds; only then do we hard-fail with `PlanValidationError`.
- `handoff_to_user_required` capabilities are NEVER terminal: the planner
  must follow them with an `ask_user` step (Phase-A handoff round-trip).
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from agents.routing.capability_matrix_router import (
    FoundationNotApplicable,
    NoRunnableAppForCapability,
    route as route_app_capability,
)
from agents.llm.llm_retry import create_with_retry
from agents.routing.route_overlay import RouteOverlay, compute_route_key as _compute_route_key
from agents.routing.locale_policy import (
    appears_compatible_with_locale,
    first_locale,
    has_explicit_language_instruction,
    language_label,
    locale_policy_text,
)

# `_VAR_RE` (the `{var}` / `{var.field}` matcher) and the fenced-JSON parser
# are shared with the runner so the planner validates exactly what the runner
# will later template against.
from agents.flow.flow_runner_util import _VAR_RE, _parse_fenced_json

# MobileWorld fallback + plan-shape/template helpers split out of this module;
# re-exported here so `flow_planner` stays the public facade (tests import
# MW_STEP_TYPE / _to_mw_leg / _mw_whole_request_plan from here, and the class
# below uses every name as a module global).
from agents.flow.flow_planner_mw import (
    MW_STEP_TYPE,
    _mw_whole_request_plan,
    _to_mw_leg,
    mw_fallback_enabled,
)
from agents.flow.flow_planner_util import (
    _bind_referenced_later,
    _fill_template,
    _has_slot_value,
    _is_ask_user,
    _is_mw_leg,
    _var_roots,
)


class PlanValidationError(RuntimeError):
    """Raised when a synthesized plan fails local validation.

    Carries the offending plan and the concrete error list so the caller can
    surface them (and so a future repair loop can feed them back to the LLM).
    """

    def __init__(
        self,
        nl_request: str,
        plan: Any,
        errors: list[str],
        *,
        coverage_gaps: list[str] | None = None,
    ) -> None:
        self.nl_request = nl_request
        self.plan = plan
        self.errors = errors
        self.coverage_gaps: list[str] = list(coverage_gaps or [])
        joined = "\n  - ".join(errors)
        super().__init__(
            f"Synthesized plan failed validation ({len(errors)} error(s)):\n  - {joined}"
        )


# Max LLM repair rounds: re-prompt with the plan + its validation/routing errors
# and re-validate, before giving up with PlanValidationError.
_REPAIR_ROUNDS = 3


_PLANNER_SYSTEM = """You synthesize a multi-app cowork PLAN from a user's natural-language request.

You are given a catalog of apps. Each app has an embedded AI agent with a list
of supported locales plus a list of capabilities (id, description, example_prompts, executable,
handoff_to_user_required, x_skip_wait_for_reply).

Produce ONE JSON object inside a ```json``` fence — a flow plan shaped like:

{
  "description": "<one-line summary of the plan>",
  "apps_required": [{"app_id": "<id>", "use_capability": "<cap id>"}, ...],
  "steps": [ <step>, ... ]
}

A step is ONE of:

  App step (drives one app's agent for one capability):
    {
      "id": "<unique short id>",
      "app": "<optional/provisional app_id from the catalog>",
      "capability": "<optional/provisional capability id that exists on that app>",
      "prompt": "<concrete text to give the in-app agent; may reference {var} or {var.field} bound by an EARLIER step>",
      "extract": {                       // OPTIONAL — only when a LATER step consumes structured data from this reply
        "prompt": "<instruction to parse the captured reply into JSON>",
        "bind_to_array_key": "<key to pull out of the extracted JSON object>"
      },
      "bind": "<var name to store this step's result>"   // OPTIONAL — omit if nothing downstream needs it
    }

  Ask-user step (hand control to the human, then continue):
    {
      "id": "<unique short id>",
      "type": "ask_user",
      "bind": "<var name to store the user's choice/answer>",
      "prompt_header": "<what to show the user; may reference {var}>",
      "select_from": "<bare var name (a string, NOT a list/object) that an EARLIER step bound to a list>",   // OPTIONAL — render a numbered pick list from that list
      "item_label": "{name}（{district}）"       // OPTIONAL — how to render each list item
    }

RULES:
1. Use ONLY app_id and capability ids that appear in the catalog. Never invent ids.
   App/capability fields are provisional: after you synthesize the step prompts,
   a separate matrix-backed router will resolve every app step's final app and
   capability. Focus on decomposing the task and writing each concrete step prompt.
2. To pass data between steps, give the upstream app step an `extract` + a `bind`,
   then reference it downstream as {var} or {var.field}. EVERY {var} you reference
   MUST be produced by an EARLIER step's bind.
3. When a step's reply is a LIST the user should choose from, insert an ask_user
   step with `select_from` so they can pick one. `select_from` MUST be a bare var
   NAME (a plain string, never a list or object literal) that an EARLIER step
   already bound to that list. So if the list comes from reading/searching
   something (a calendar, an inbox, a product search), you MUST first add that
   read/search app step with an `extract` + `bind` that produces the list, and
   point `select_from` at that bind. Never reference a var in `select_from` (or
   `{var}`) that no earlier step binds.
4. If a capability has "handoff_to_user_required": true and it is NOT the final
   action of the whole task, you MUST follow it with an ask_user step (show the
   agent's surfaced reply via prompt_header, collect the user's answer), then
   continue with another app step that consumes the answer — re-state the FULL
   intent in that following step's prompt, because it runs as a fresh agent
   session. If such a capability IS the final action (e.g. hailing the ride at
   the very end), it MAY be the last step: its own in-app handoff is the user's
   final confirmation, so no trailing ask_user is needed.
   If a capability has "x_skip_wait_for_reply": true, that app step does NOT
   capture a text reply. Do not add `bind` or `extract` for it unless another
   step can get the needed data from the user through an ask_user step.
5. Prefer the user's own wording in prompts; expand only to fill obvious gaps.
   Bake concrete values from the request directly into the prompts.
   For each app-step `prompt`, default to that app's first `locale` language.
   If the user's request or the prompt explicitly asks for a different language,
   honor that explicit instruction. Preserve proper nouns, addresses, product
   names, code, URLs, emails, ids, and quoted literal text in their original
   language.
6. A single-app request is fine — emit a one-step plan.
7. A `foundation_llm` capability is a GENERAL knowledge / reasoning / text
   capability. For information, Q&A, summarization, drafting, explanation, or
   lookup tasks that no dedicated app capability covers (e.g. explaining a GitHub
   repo or project, summarizing an arXiv paper, answering a general question),
   route them to a `foundation_llm` capability rather than declaring the request
   unsatisfiable.
8. Only return unsatisfiable when the task REQUIRES a concrete device/app action
   that no capability provides (e.g. posting to a specific chat platform the
   catalog lacks, taking a camera photo, controlling an app not in the catalog)
   AND `foundation_llm` cannot stand in:
   return {"unsatisfiable": true, "reason": "<short explanation>"}.

No prose outside the fence."""


_LOCALIZE_PROMPT_SYSTEM = """You rewrite one mobile in-app agent prompt to match an app locale.

Rules:
- If the original user request or current prompt explicitly asks for a language,
  return the current prompt unchanged.
- Otherwise rewrite only the natural-language instruction text into the target
  locale's language.
- Preserve placeholders like {var} and {var.field} byte-for-byte.
- Preserve proper nouns, addresses, product names, code, URLs, emails, ids, and
  quoted literal text in their original language.
- Do not add new requirements or remove existing requirements.

Return ONE JSON object inside a ```json``` fence:
{"prompt": "<final prompt>", "changed": true, "reason": "<short reason>"}
"""


_SLOT_EXTRACT_SYSTEM = """You extract slot values to fill ONE fixed in-app-agent prompt template.

You are given a capability's prompt template (e.g. "Navigate to {place}."), its
slot specs (name, desc, required), the original user request, the planner's
synthesized prompt for this step, and a list of upstream variables that earlier
steps already produced.

Your ONLY job is to extract each slot's value. Do NOT rephrase the template, do
NOT translate, do NOT add slots that are not listed.

Rules:
- Prefer the user's own wording from the original request / synthesized prompt.
  Keep proper nouns, addresses, place/product names, ids, URLs as-is.
- If a slot's value should come from an EARLIER step's result, return that
  variable as a literal placeholder token like "{poi.name}" — use ONLY names
  from `referencable_upstream_vars`. Never invent a variable.
- If a REQUIRED slot's value is genuinely absent from the request, leave it out
  of `slots` and list its name in `missing` (do NOT guess or hallucinate).
- An OPTIONAL slot with no value: just omit it from `slots`.
- The template may contain `[ ... {slot} ... ]` OPTIONAL segments. Extract the
  inner `{slot}` normally when the request supplies it; when it does not, just
  omit that slot — the segment (its surrounding wording included) is dropped
  automatically. Never emit the literal `[` / `]` brackets as part of a value.

Return ONE JSON object inside a ```json``` fence:
{"slots": {"<slot name>": "<value or {var} token>", ...}, "missing": ["<required slot name>", ...]}
"""


class PromptTemplateError(RuntimeError):
    """Raised when a capability's prompt template cannot be filled deterministically.

    Surfaced as a plan validation error (hard fail) so a residual/hallucinated
    prompt is never submitted to the in-app agent.
    """


class FlowPlanner:
    def __init__(
        self,
        catalog: dict[str, Any],
        llm: Any,
        model: str,
        *,
        matrix: dict[str, Any] | None = None,
        mw_fallback: bool | None = None,
    ) -> None:
        self.catalog = catalog
        self._llm = llm
        self._model = model
        self.matrix = matrix
        # When True, a leg RA can't cover (or a request judged unsatisfiable) is
        # handed to MobileWorld instead of failing the plan.
        self.mw_fallback = mw_fallback_enabled() if mw_fallback is None else mw_fallback
        # Trace-guided route solidification: a confidently-successful route is
        # short-circuited here (0 LLM); leg verdicts feed it from FlowRunner.
        self._overlay = RouteOverlay()
        self._apps_by_id = {
            app.get("app_id"): app for app in catalog.get("apps", []) if app.get("app_id")
        }
        # app_id -> {capability_id -> capability dict}, for id validation and
        # the handoff_to_user_required lookup.
        self._caps: dict[str, dict[str, dict]] = {}
        for app in catalog.get("apps", []):
            app_id = app.get("app_id")
            if not app_id:
                continue
            self._caps[app_id] = {
                c["id"]: c for c in (app.get("capabilities") or []) if c.get("id")
            }

    # ------------------------------------------------------------------ plan

    def plan(self, nl_request: str) -> dict[str, Any]:
        """Synthesize and validate a plan for `nl_request`.

        Returns the plan dict (flow yaml shape) on success, or a
        `{"unsatisfiable": true, "reason": ...}` dict if the LLM judged the
        request unsatisfiable with the available apps. Raises
        PlanValidationError if the synthesized plan does not validate.
        """
        user = (
            "Available apps catalog:\n"
            f"{json.dumps(self.catalog, ensure_ascii=False, indent=2)}\n\n"
            f"User request:\n{nl_request}\n\n"
            "Synthesize the plan JSON now."
        )
        resp = create_with_retry(self._llm,
            model=self._model,
            messages=[
                {"role": "system", "content": _PLANNER_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=2048,
        )
        raw = (resp.choices[0].message.content or "").strip()
        logger.debug(f"planner raw reply: {raw}")
        data = _parse_fenced_json(raw)
        if not isinstance(data, dict):
            raise PlanValidationError(
                nl_request, data, [f"planner returned a {type(data).__name__}, expected an object"]
            )
        if data.get("unsatisfiable"):
            reason = data.get("reason")
            if self.mw_fallback:
                logger.info(f"planner: unsatisfiable — {reason!r}; MobileWorld fallback (whole request)")
                return _mw_whole_request_plan(nl_request, str(reason or "no app covers this request"))
            logger.info(f"planner: unsatisfiable — {reason!r}")
            return data

        # Route → validate → repair loop. resolve_app_routes raises
        # PlanValidationError on routing errors; _validate returns validation
        # errors. Either kind feeds an LLM repair round before we give up — up
        # to `_REPAIR_ROUNDS` re-prompts.
        for attempt in range(_REPAIR_ROUNDS + 1):
            errors: list[str] = []
            coverage_gaps: list[str] = []
            if self.matrix is not None:
                try:
                    data = self.resolve_app_routes(data, nl_request)
                except PlanValidationError as e:
                    errors = list(e.errors)
                    coverage_gaps = list(e.coverage_gaps)
            if not errors:
                errors = self._validate(data)
            if not errors:
                n_legs = sum(1 for s in data["steps"] if not _is_ask_user(s))
                if n_legs >= 4:
                    # No hard cap (by decision), but a long plan is worth flagging.
                    logger.warning(f"synthesized plan has {n_legs} app legs — review the preview carefully")
                tail = f" after {attempt} repair round(s)" if attempt else ""
                logger.info(f"planner: {len(data['steps'])} steps ({n_legs} app legs){tail}")
                return data
            if attempt == _REPAIR_ROUNDS:
                # If repair never managed to avoid a capability that no app
                # provides, the request is unsatisfiable with the current apps —
                # report that rather than a validation failure.
                if coverage_gaps:
                    reason = (
                        "Required capability has no app authorized in the catalog: "
                        + "; ".join(coverage_gaps)
                    )
                    if self.mw_fallback:
                        return self._apply_mw_fallback_to_gaps(data, nl_request, reason)
                    logger.info(f"planner: unsatisfiable (coverage gap) — {reason}")
                    return {"unsatisfiable": True, "reason": reason}
                # No coverage gap, but the plan still fails validation after all
                # repair rounds (e.g. an unfillable prompt template, a handoff
                # structure RA can't satisfy). With MW fallback on, don't give
                # up: hand the whole request to MobileWorld rather than failing.
                if self.mw_fallback:
                    reason = "plan failed validation after repair: " + "; ".join(errors)
                    logger.info(
                        f"planner: unrepairable plan — {reason!r}; MobileWorld fallback (whole request)"
                    )
                    return _mw_whole_request_plan(nl_request, reason)
                raise PlanValidationError(nl_request, data, errors)
            logger.info(
                f"planner: {len(errors)} error(s); repair round {attempt + 1}/{_REPAIR_ROUNDS}: {errors}"
            )
            data = self._repair(data, errors, nl_request)
            if not isinstance(data, dict):
                raise PlanValidationError(
                    nl_request, data, [f"repair returned a {type(data).__name__}, expected an object"]
                )
            if data.get("unsatisfiable"):
                reason = data.get("reason")
                if self.mw_fallback:
                    logger.info(
                        f"planner (repair): unsatisfiable — {reason!r}; MobileWorld fallback (whole request)"
                    )
                    return _mw_whole_request_plan(nl_request, str(reason or "no app covers this request"))
                logger.info(f"planner (repair): unsatisfiable — {reason!r}")
                return data
        # unreachable (loop returns or raises), but keeps type-checkers happy
        return data

    def _apply_mw_fallback_to_gaps(
        self, plan: dict, nl_request: str, reason: str
    ) -> dict:
        """Convert every coverage-gap step (tagged by `_route_one_step`) into a
        MobileWorld fallback leg, then re-validate the now-satisfiable plan.

        Repair has already had its rounds to re-route the gap to a real
        capability (preferred); this is the last resort so the plan runs instead
        of failing. Any non-gap step keeps its resolved app/capability."""
        converted = [
            _to_mw_leg(step, step.get("x_coverage_gap") or reason)["id"]
            for step in plan.get("steps", [])
            if isinstance(step, dict) and step.get("x_coverage_gap")
        ]
        logger.info(
            f"planner: coverage gap unrepaired — MobileWorld fallback for leg(s) {converted}"
        )
        self._refresh_apps_required(plan)
        errors = self._validate(plan)
        if errors:
            # The gap-leg fallback itself produced an invalid plan (e.g. a
            # downstream {var} that only the dropped capability could have
            # bound). This method only runs with MW fallback on, so don't give
            # up: hand the whole request to MobileWorld rather than running a
            # broken plan or reporting unsatisfiable.
            logger.warning(
                f"planner: MobileWorld gap-fallback plan still invalid: {errors}; "
                "MobileWorld fallback (whole request)"
            )
            return _mw_whole_request_plan(nl_request, reason)
        return plan

    def validate_plan(self, plan: dict, nl_request: str) -> None:
        errors = self._validate(plan)
        if errors:
            # The synthesis path (`plan`) repairs via an LLM round before this
            # raises; direct callers (e.g. a cached plan) still hard-fail so a
            # malformed plan never silently executes.
            raise PlanValidationError(nl_request, plan, errors)

    # --------------------------------------------------------------- routing

    def resolve_app_routes(self, plan: dict, nl_request: str) -> dict:
        """Fill each app step's app/capability via the shared three-stage router."""
        if self.matrix is None:
            return plan

        steps = plan.get("steps")
        if not isinstance(steps, list):
            return plan

        errors: list[str] = []
        # Coverage gaps: steps whose matched capability has no app at all. These
        # are surfaced separately so `plan` can classify the request as
        # unsatisfiable rather than as a (repairable) validation error.
        gaps: list[str] = []
        # Vars bound by earlier steps (app steps + ask_user), in plan order, so a
        # templated step's slot can legitimately reference an upstream {var}.
        produced: set[str] = set()
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            # MobileWorld fallback legs have no app/capability to route (a cached
            # plan can already carry them); skip routing, like ask_user.
            if not _is_ask_user(step) and not _is_mw_leg(step):
                self._route_one_step(step, i, nl_request, produced, errors, gaps)
            bind = step.get("bind")
            # Only track string binds; a non-string bind (the LLM occasionally
            # emits a list/object) is left for `_validate` to flag — adding it
            # here would crash on the unhashable value before validation runs.
            if bind and isinstance(bind, str):
                produced.add(bind)

        if errors:
            raise PlanValidationError(nl_request, plan, errors, coverage_gaps=gaps)

        self._drop_unused_no_reply_binds(plan)
        self._refresh_apps_required(plan)
        return plan

    def _route_one_step(
        self,
        step: dict,
        i: int,
        nl_request: str,
        produced: set[str],
        errors: list[str],
        gaps: list[str],
    ) -> None:
        """Route one app step, then fill its prompt (template or localize)."""
        prompt = step.get("prompt")
        if not prompt:
            return

        # Solidification key (RELAY_ROUTE_KEY_MODE; default value-independent B
        # off the planner's provisional capability + app hint + request locale,
        # so "navigate to A/B" share one route). Stashed on the step so
        # FlowRunner can attribute the leg's verdict back to the same key. Reuse
        # a key persisted by an earlier (fresh) build so a cache-hit re-run —
        # where `prompt` is already filled and `capability` is the final routed
        # one, not the provisional — keeps the SAME key instead of recomputing.
        key = step.get("x_route_key") or _compute_route_key(
            prompt,
            provisional_cap=step.get("capability"),
            provisional_app=step.get("app"),
        )
        step["x_route_key"] = key

        request = self._route_request_for_step(step, nl_request)
        try:
            decision = route_app_capability(
                request,
                self.catalog,
                self.matrix,
                self._llm,
                self._model,
                preserve_goal=True,
                route_key=key,
                overlay=self._overlay,
            )
        except NoRunnableAppForCapability as e:
            # Coverage gap: the matched capability has no app. Record it both as
            # an error (so a repair round can try to re-route, e.g. to
            # foundation_llm) and as a gap (so an unrepaired plan is classified
            # unsatisfiable rather than invalid). Tag the step too, so that if
            # repair never closes the gap, `_apply_mw_fallback_to_gaps` can turn
            # exactly these steps into MobileWorld legs.
            msg = f"step {step.get('id') or i!r}: route failed: {e}"
            errors.append(msg)
            gaps.append(msg)
            step["x_coverage_gap"] = str(e)
            return
        except FoundationNotApplicable as e:
            # The request needs a concrete on-device action a chat assistant
            # can't perform, and no vertical capability matched either. This is
            # NOT a foundation task: treat it as a coverage gap so repair can
            # retry and, failing that, `_apply_mw_fallback_to_gaps` routes it to
            # MobileWorld instead of force-fitting it into foundation_llm.
            msg = f"step {step.get('id') or i!r}: not a foundation task: {e}"
            errors.append(msg)
            gaps.append(msg)
            step["x_coverage_gap"] = str(e)
            return
        except Exception as e:  # surfaced as a plan validation error
            errors.append(f"step {step.get('id') or i!r}: route failed: {e}")
            return

        app_id = decision.get("app_id")
        cap_id = decision.get("capability_id")
        if app_id:
            step["app"] = app_id
        if cap_id:
            step["capability"] = cap_id
        if app_id:
            cap_meta = self._caps.get(app_id, {}).get(cap_id, {})
            if cap_meta.get("prompt_template"):
                # Deterministic submit prompt: extract slots, fill the fixed
                # template, skip locale rewrite (the template is authored in the
                # app's target locale). Any failure is a hard plan error.
                try:
                    step["prompt"] = self._fill_prompt_template(
                        cap_meta, step["prompt"], nl_request, produced
                    )
                except Exception as e:
                    errors.append(f"step {step.get('id') or i!r}: {e}")
                    return
            else:
                step["prompt"] = self._maybe_localize_prompt(
                    step["prompt"], app_id, nl_request
                )
        logger.info(
            f"planner route step {step.get('id') or i!r} -> "
            f"{app_id} / {cap_id} (reason: {decision.get('reason')})"
        )

    def _fill_prompt_template(
        self,
        cap_meta: dict,
        synthesized_prompt: str,
        nl_request: str,
        produced: set[str],
    ) -> str:
        """Fill a capability's `prompt_template` from extracted slot values.

        The LLM extracts ONLY slot values (not phrasing); the template fixes the
        wording so the in-app agent's intent routing isn't subject to phrasing
        drift. A missing required slot or an out-of-scope `{var}` is a hard error
        (raises PromptTemplateError) so a residual prompt never reaches the app.
        """
        template = cap_meta["prompt_template"]
        slot_specs = cap_meta.get("prompt_slots") or []
        slot_names = [s.get("name") for s in slot_specs if s.get("name")]
        # Offer exactly the vars earlier steps actually produced — the same set
        # the post-fill stray-var guard validates against — so a compliant
        # extractor can never emit a var the guard will then hard-reject.
        allowed_vars = sorted(produced)

        user = json.dumps(
            {
                "template": template,
                "slots": [
                    {
                        "name": s.get("name"),
                        "desc": s.get("desc", ""),
                        "required": s.get("required", True),
                    }
                    for s in slot_specs
                ],
                "original_user_request": nl_request,
                "synthesized_prompt": synthesized_prompt,
                "referencable_upstream_vars": allowed_vars,
            },
            ensure_ascii=False,
            indent=2,
        )
        resp = create_with_retry(self._llm,
            model=self._model,
            messages=[
                {"role": "system", "content": _SLOT_EXTRACT_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=512,
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = _parse_fenced_json(raw)
        if not isinstance(data, dict):
            raise PromptTemplateError(
                f"slot extractor returned {type(data).__name__}, expected object "
                f"(template {template!r})"
            )
        slots = data.get("slots") or {}
        if not isinstance(slots, dict):
            raise PromptTemplateError(f"slot extractor `slots` is not an object (template {template!r})")

        missing = [
            s.get("name")
            for s in slot_specs
            if s.get("required", True) and not _has_slot_value(slots, s.get("name"))
        ]
        if missing:
            raise PromptTemplateError(
                f"required slot(s) {missing} could not be extracted for template {template!r}"
            )

        filled = _fill_template(template, slots, slot_names)

        # {var} guard (same intent as localize): any {var} surviving in the
        # filled prompt must be an upstream-produced bind, else the runtime
        # render() would silently drop it to ''.
        stray = _var_roots(filled) - produced
        if stray:
            raise PromptTemplateError(
                f"filled template references unbound vars {sorted(stray)}: {filled!r}"
            )
        logger.info(f"templated submit prompt via {template!r}: {filled!r}")
        return filled

    def _drop_unused_no_reply_binds(self, plan: dict) -> None:
        """Remove stale binds from steps that cannot capture text replies.

        A cached or LLM-synthesized terminal handoff sometimes includes a
        decorative `bind` even though the action plan skips wait_for_reply. If
        nothing downstream references that value, dropping it lets the runner
        treat the leg as a pure handoff. If it is referenced, validation below
        keeps the plan from executing with missing data.
        """
        steps = plan.get("steps")
        if not isinstance(steps, list):
            return

        for idx, step in enumerate(steps):
            if not isinstance(step, dict) or _is_ask_user(step):
                continue
            # A falsy `bind` (null/"") is never meaningful — strip the key so it
            # doesn't persist into the plan and land as a None blackboard key.
            if "bind" in step and not step.get("bind"):
                step.pop("bind", None)
                step.pop("extract", None)
                continue
            bind = step.get("bind")
            if not bind:
                continue
            cap_meta = self._caps.get(step.get("app"), {}).get(step.get("capability"), {})
            if not cap_meta.get("x_skip_wait_for_reply"):
                continue
            if _bind_referenced_later(bind, steps, idx + 1):
                continue
            step.pop("bind", None)
            step.pop("extract", None)
            logger.info(
                f"planner dropped unused bind {bind!r} from no-reply step "
                f"{step.get('id')!r}"
            )

    def _maybe_localize_prompt(
        self,
        prompt: str,
        app_id: str,
        nl_request: str,
    ) -> str:
        app = self._apps_by_id.get(app_id) or {}
        locale = first_locale(app)
        if not locale:
            return prompt
        if has_explicit_language_instruction(prompt) or has_explicit_language_instruction(nl_request):
            return prompt
        if appears_compatible_with_locale(prompt, locale):
            return prompt

        orig_vars = set(_VAR_RE.findall(prompt or ""))
        user = json.dumps(
            {
                "target_app": {
                    "app_id": app_id,
                    "app_name": app.get("app_name"),
                    "locale": app.get("locale") or [],
                },
                "target_language": language_label(locale),
                "locale_policy": locale_policy_text(locale),
                "original_user_request": nl_request,
                "current_prompt": prompt,
            },
            ensure_ascii=False,
            indent=2,
        )
        try:
            resp = create_with_retry(self._llm,
                model=self._model,
                messages=[
                    {"role": "system", "content": _LOCALIZE_PROMPT_SYSTEM},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
                max_tokens=1024,
            )
            raw = (resp.choices[0].message.content or "").strip()
            data = _parse_fenced_json(raw)
            rewritten = str(data.get("prompt") or "").strip()
        except Exception as e:
            logger.warning(f"prompt locale rewrite failed for {app_id}: {e}")
            return prompt

        if not rewritten:
            return prompt
        new_vars = set(_VAR_RE.findall(rewritten))
        if new_vars != orig_vars:
            logger.warning(
                f"prompt locale rewrite for {app_id} changed placeholders "
                f"{sorted(orig_vars)} -> {sorted(new_vars)}; keeping original"
            )
            return prompt
        logger.info(
            f"localized step prompt for {app_id} to {locale}: "
            f"{prompt!r} -> {rewritten!r}"
        )
        return rewritten

    def _route_request_for_step(self, step: dict, nl_request: str) -> str:
        parts = [
            "Route only this planned app step, not the whole flow.",
            f"Original end-to-end request: {nl_request}",
            f"Planned app-step prompt: {step.get('prompt', '')}",
        ]
        if step.get("app") or step.get("capability"):
            parts.append(
                "Planner's provisional hint: "
                f"app={step.get('app') or ''}, capability={step.get('capability') or ''}"
            )
        return "\n".join(parts)

    def _refresh_apps_required(self, plan: dict) -> None:
        seen: set[tuple[str, str]] = set()
        apps_required: list[dict[str, str]] = []
        for step in plan.get("steps", []):
            if not isinstance(step, dict) or _is_ask_user(step):
                continue
            app = step.get("app")
            cap = step.get("capability")
            if not app or not cap or (app, cap) in seen:
                continue
            seen.add((app, cap))
            apps_required.append({"app_id": app, "use_capability": cap})
        plan["apps_required"] = apps_required

    # --------------------------------------------------------------- repair

    def _repair(self, plan: dict, errors: list[str], nl_request: str) -> Any:
        """Re-prompt the LLM with the broken plan + its validation/routing errors
        and return a corrected plan dict (same schema). May return an
        `{"unsatisfiable": ...}` dict. The caller re-routes and re-validates the
        result, so a still-broken repair just consumes another round."""
        user = (
            "Available apps catalog:\n"
            f"{json.dumps(self.catalog, ensure_ascii=False, indent=2)}\n\n"
            f"User request:\n{nl_request}\n\n"
            "Your previous plan FAILED validation. Previous plan:\n"
            f"{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
            "Errors to fix:\n"
            + "\n".join(f"- {e}" for e in errors)
            + "\n\nReturn a CORRECTED plan JSON (same schema, inside a ```json fence) "
            "that fixes ALL of these errors; keep the parts that were fine. Common "
            "fixes: add the missing ask_user step after a handoff capability; make "
            "`select_from` a plain var name bound by an earlier step; bind every "
            "{var} you reference upstream. If the request truly cannot be satisfied, "
            'return {"unsatisfiable": true, "reason": "<why>"} instead.'
        )
        resp = create_with_retry(self._llm,
            model=self._model,
            messages=[
                {"role": "system", "content": _PLANNER_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=2048,
        )
        raw = (resp.choices[0].message.content or "").strip()
        logger.debug(f"repair raw reply: {raw}")
        return _parse_fenced_json(raw)

    # ------------------------------------------------------------- validate

    def _validate(self, plan: dict) -> list[str]:
        """Return a list of human-readable validation errors ([] = valid)."""
        errors: list[str] = []
        steps = plan.get("steps")
        if not isinstance(steps, list) or not steps:
            return ["plan has no non-empty `steps` list"]

        produced: set[str] = set()  # vars bound by steps seen so far
        seen_ids: set[str] = set()
        seen_binds: set[str] = set()

        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                errors.append(f"step #{i} is not an object")
                continue
            sid = step.get("id") or f"#{i}"
            # Coerce the id to a string before the dup check so a non-string id
            # (list/object from the LLM) can't crash the unhashable set ops.
            sid_key = sid if isinstance(sid, str) else json.dumps(sid, sort_keys=True)
            if sid_key in seen_ids:
                errors.append(f"duplicate step id {step.get('id')!r}")
            seen_ids.add(sid_key)

            if _is_ask_user(step):
                self._validate_ask_user(step, sid, produced, errors)
            elif _is_mw_leg(step):
                self._validate_mw_leg(step, sid, produced, errors)
            else:
                self._validate_app_step(step, sid, steps, i, produced, errors)

            bind = step.get("bind")
            if bind is not None and not isinstance(bind, str):
                # Guard against an unhashable bind (the LLM occasionally emits a
                # list/object); record it so the repair round fixes it instead
                # of crashing the set membership / add below.
                errors.append(
                    f"step {sid!r}: bind must be a single var name string, "
                    f"got {type(bind).__name__}"
                )
            elif bind:
                if bind in seen_binds:
                    errors.append(f"step {sid!r}: duplicate bind name {bind!r}")
                seen_binds.add(bind)
                produced.add(bind)

        return errors

    def _validate_mw_leg(
        self, step: dict, sid: str, produced: set[str], errors: list[str],
    ) -> None:
        """A MobileWorld fallback leg needs no app/capability (it's manifest-free
        general_e2e), but still needs a prompt and bound upstream {var}s."""
        prompt = step.get("prompt")
        if not prompt:
            errors.append(f"step {sid!r}: missing `prompt`")
        refs = _var_roots(prompt or "")
        if isinstance(step.get("extract"), dict):
            refs |= _var_roots(step["extract"].get("prompt", ""))
        for r in sorted(refs - produced):
            errors.append(f"step {sid!r}: references {{{r}}} before it is bound")

    def _validate_app_step(
        self, step: dict, sid: str, steps: list, idx: int,
        produced: set[str], errors: list[str],
    ) -> None:
        app = step.get("app")
        cap = step.get("capability")
        prompt = step.get("prompt")
        if not app:
            errors.append(f"step {sid!r}: missing `app`")
        if not cap:
            errors.append(f"step {sid!r}: missing `capability`")
        if not prompt:
            errors.append(f"step {sid!r}: missing `prompt`")

        if app and app not in self._caps:
            errors.append(f"step {sid!r}: unknown app_id {app!r}")
        elif app and cap and cap not in self._caps[app]:
            known = sorted(self._caps[app])
            errors.append(f"step {sid!r}: unknown capability {cap!r} for {app!r} (known: {known})")

        # {var} references in prompt / extract.prompt must already be produced.
        refs = _var_roots(prompt or "")
        if isinstance(step.get("extract"), dict):
            refs |= _var_roots(step["extract"].get("prompt", ""))
        for r in sorted(refs - produced):
            errors.append(f"step {sid!r}: references {{{r}}} before it is bound")

        # Rule 4: a mid-flow handoff_to_user_required leg must be followed by an
        # ask_user step. A handoff leg that is the LAST step is fine as a
        # terminal — its own in-app handoff is the user's final confirmation
        # (e.g. a plan that ends on hailing a ride).
        cap_meta = self._caps.get(app, {}).get(cap, {}) if app else {}
        if cap_meta.get("handoff_to_user_required") and idx != len(steps) - 1:
            nxt = steps[idx + 1]
            if not (isinstance(nxt, dict) and _is_ask_user(nxt)):
                errors.append(
                    f"step {sid!r}: capability {cap!r} is handoff_to_user_required and is "
                    f"not the final step — it must be followed by an ask_user step"
                )
        if cap_meta.get("x_skip_wait_for_reply") and (step.get("bind") or step.get("extract")):
            errors.append(
                f"step {sid!r}: capability {cap!r} skips reply capture, so it cannot use "
                "`bind` or `extract`; collect needed data with ask_user instead"
            )

    def _validate_ask_user(
        self, step: dict, sid: str, produced: set[str], errors: list[str],
    ) -> None:
        if not step.get("bind"):
            errors.append(f"ask_user step {sid!r}: missing `bind`")
        sel = step.get("select_from")
        if sel is not None:
            if not isinstance(sel, str):
                # The LLM occasionally emits a list/object here; record it as a
                # validation error (the repair round fixes it) instead of letting
                # an unhashable value crash the `in produced` membership test.
                errors.append(
                    f"ask_user step {sid!r}: select_from must be a single var name "
                    f"string, got {type(sel).__name__}"
                )
            else:
                # select_from may be written as `{var}` or `var.field`; resolve to
                # its root bind name before checking it was produced upstream.
                root = sel.strip("{}").split(".")[0].strip()
                if root and root not in produced:
                    errors.append(
                        f"ask_user step {sid!r}: select_from {sel!r} is not bound by an earlier step"
                    )
        for r in sorted(_var_roots(step.get("prompt_header", "")) - produced):
            errors.append(f"ask_user step {sid!r}: prompt_header references {{{r}}} before it is bound")

