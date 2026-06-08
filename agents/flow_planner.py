"""LLM flow synthesizer.

Given the full catalog of apps + their embedded-agent capabilities (the
same shape `agents.card_catalog.build_catalog()` produces) and a user's
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
  binds). On failure we hard-fail with the error list — an LLM repair loop is a
  deliberate TODO (see `_repair`), not yet wired.
- `handoff_to_user_required` capabilities are NEVER terminal: the planner
  must follow them with an `ask_user` step (Phase-A handoff round-trip).
"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger
from openai import OpenAI

from agents.capability_matrix_router import route as route_app_capability
from agents.route_overlay import RouteOverlay, compute_route_key as _compute_route_key
from agents.locale_policy import (
    appears_compatible_with_locale,
    first_locale,
    has_explicit_language_instruction,
    language_label,
    locale_policy_text,
)

# `_VAR_RE` (the `{var}` / `{var.field}` matcher) and the fenced-JSON parser
# are shared with the runner so the planner validates exactly what the runner
# will later template against.
from agents.flow_runner import _VAR_RE, _parse_fenced_json


class PlanValidationError(RuntimeError):
    """Raised when a synthesized plan fails local validation.

    Carries the offending plan and the concrete error list so the caller can
    surface them (and so a future repair loop can feed them back to the LLM).
    """

    def __init__(self, nl_request: str, plan: Any, errors: list[str]) -> None:
        self.nl_request = nl_request
        self.plan = plan
        self.errors = errors
        joined = "\n  - ".join(errors)
        super().__init__(
            f"Synthesized plan failed validation ({len(errors)} error(s)):\n  - {joined}"
        )


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
      "select_from": "<var holding a list>",   // OPTIONAL — render a numbered pick list from that list
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
   step with `select_from` so they can pick one.
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
7. If NO combination of the available apps/capabilities can satisfy the request,
   return instead: {"unsatisfiable": true, "reason": "<short explanation>"}.

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
        llm: OpenAI,
        model: str,
        *,
        matrix: dict[str, Any] | None = None,
    ) -> None:
        self.catalog = catalog
        self._llm = llm
        self._model = model
        self.matrix = matrix
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
        resp = self._llm.chat.completions.create(
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
            logger.info(f"planner: unsatisfiable — {data.get('reason')!r}")
            return data

        if self.matrix is not None:
            data = self.resolve_app_routes(data, nl_request)

        self.validate_plan(data, nl_request)

        n_legs = sum(1 for s in data["steps"] if not _is_ask_user(s))
        if n_legs >= 4:
            # No hard cap (by decision), but a long plan is worth flagging.
            logger.warning(f"synthesized plan has {n_legs} app legs — review the preview carefully")
        logger.info(f"planner: {len(data['steps'])} steps ({n_legs} app legs)")
        return data

    def validate_plan(self, plan: dict, nl_request: str) -> None:
        errors = self._validate(plan)
        if errors:
            # TODO(repair): feed `errors` back to the LLM and retry (max ~2
            # rounds) before giving up. Deferred by design — for now we
            # hard-fail so a malformed plan never silently executes.
            self._repair(plan, errors)  # no-op stub; see below
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
        # Vars bound by earlier steps (app steps + ask_user), in plan order, so a
        # templated step's slot can legitimately reference an upstream {var}.
        produced: set[str] = set()
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            if not _is_ask_user(step):
                self._route_one_step(step, i, nl_request, produced, errors)
            bind = step.get("bind")
            if bind:
                produced.add(bind)

        if errors:
            raise PlanValidationError(nl_request, plan, errors)

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
        resp = self._llm.chat.completions.create(
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
            resp = self._llm.chat.completions.create(
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

    def _repair(self, plan: dict, errors: list[str]) -> None:
        """TODO(repair): re-prompt the LLM with `errors` to fix `plan`, up to
        ~2 rounds, and return the repaired plan. Intentionally a no-op for now
        — validation failures hard-fail via PlanValidationError."""
        return None

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
            if step.get("id") in seen_ids:
                errors.append(f"duplicate step id {step.get('id')!r}")
            seen_ids.add(step.get("id"))

            if _is_ask_user(step):
                self._validate_ask_user(step, sid, produced, errors)
            else:
                self._validate_app_step(step, sid, steps, i, produced, errors)

            bind = step.get("bind")
            if bind:
                if bind in seen_binds:
                    errors.append(f"step {sid!r}: duplicate bind name {bind!r}")
                seen_binds.add(bind)
                produced.add(bind)

        return errors

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
        if sel and sel not in produced:
            errors.append(f"ask_user step {sid!r}: select_from {sel!r} is not bound by an earlier step")
        for r in sorted(_var_roots(step.get("prompt_header", "")) - produced):
            errors.append(f"ask_user step {sid!r}: prompt_header references {{{r}}} before it is bound")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _is_ask_user(step: dict) -> bool:
    return isinstance(step, dict) and step.get("type") == "ask_user"


def _var_roots(template: str) -> set[str]:
    """Root variable names referenced by `{var}` / `{var.field}` in a template."""
    return {m.group(1).split(".")[0] for m in _VAR_RE.finditer(template or "")}


# Optional template segment: `[ ... {slot} ... ]`. Non-nested. Kept (brackets
# stripped, inner slots filled) only when every declared slot it references has a
# non-empty value; otherwise the whole segment — surrounding wording, spaces, and
# punctuation included — is dropped.
_OPT_SEGMENT_RE = re.compile(r"\[([^\[\]]*)\]")


def _has_slot_value(slots: dict, name: str) -> bool:
    v = slots.get(name)
    return v is not None and str(v).strip() != ""


def _fill_slots(text: str, slots: dict, slot_names: list[str]) -> str:
    """Replace each declared `{slot}` with its value, leaving other braces intact.

    Targeted replacement (not str.format) so cross-step `{var}` tokens that a slot
    value carries (e.g. "{poi.name}") survive for the runtime `render()`.
    """
    for name in slot_names:
        val = slots.get(name)
        text = text.replace("{" + name + "}", "" if val is None else str(val))
    return text


def _fill_template(template: str, slots: dict, slot_names: list[str]) -> str:
    """Fill declared `{slot}`s; conditionally render optional `[..]` segments.

    Template syntax:
    - `{slot}` — replaced by its extracted value (or an upstream `{var}` token).
    - `[ ... {slot} ... ]` — an OPTIONAL segment, for slots declared
      `required: false`. Kept (brackets stripped, inner slots filled) only when
      every declared slot it references has a non-empty value; otherwise the
      whole segment is removed, so surrounding wording/spaces/punctuation go with
      it (e.g. `Navigate to {place}[ by {mode}].` → `Navigate to X.` when `mode`
      is absent). A `[...]` with no declared slot inside is left as literal text.

    A bare (un-bracketed) optional slot with no value is stripped to '' — put
    optional slots inside a `[..]` segment to drop their surrounding wording too.
    """
    def _render_segment(m: re.Match) -> str:
        inner = m.group(1)
        referenced = [n for n in slot_names if ("{" + n + "}") in inner]
        if not referenced:
            return m.group(0)  # no declared slot → literal brackets, keep as-is
        if all(_has_slot_value(slots, n) for n in referenced):
            return _fill_slots(inner, slots, slot_names)
        return ""

    out = _OPT_SEGMENT_RE.sub(_render_segment, template)
    # Fill any remaining required / bare declared slots outside optional segments.
    out = _fill_slots(out, slots, slot_names)
    # Tidy double spaces a dropped mid-sentence segment can leave behind.
    out = re.sub(r"  +", " ", out).strip()
    return out


def _bind_referenced_later(bind: str, steps: list, start_idx: int) -> bool:
    for step in steps[start_idx:]:
        if not isinstance(step, dict):
            continue
        if _is_ask_user(step):
            if step.get("select_from") == bind:
                return True
            fields = [step.get("prompt_header", "")]
        else:
            fields = [step.get("prompt", "")]
            if isinstance(step.get("extract"), dict):
                fields.append(step["extract"].get("prompt", ""))
        if any(bind in _var_roots(str(field or "")) for field in fields):
            return True
    return False
