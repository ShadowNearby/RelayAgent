"""LLM flow synthesizer.

Given the full catalog of apps + their embedded-agent capabilities (the
same shape `scripts/run_nl.py:build_catalog()` produces) and a user's
natural-language request, ask the text LLM to synthesize a *new* multi-app
flow plan — the same step/bind schema that `FlowRunner` already executes,
so no new executor is needed.

Design (see CLAUDE.md + the `project_cross_app_planner` decision notes):

- Static one-shot planning: the LLM emits the whole plan once. We DON'T
  do step-by-step replanning here.
- Output reuses the flow yaml shape (`app_step` / `ask_user` / `extract` /
  `bind`); there is no `inputs` block because the request is concrete and
  literal values get baked straight into the step prompts.
- We validate the plan locally (known ids, no dangling `{var}` reference,
  handoff legs followed by ask_user, unique binds). On failure we hard-fail
  with the error list — an LLM repair loop is a deliberate TODO (see
  `_repair`), not yet wired.
- `handoff_to_user_required` capabilities are NEVER terminal: the planner
  must follow them with an `ask_user` step (Phase-A handoff round-trip).
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from openai import OpenAI

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
of capabilities (id, description, example_prompts, side_effects, executable,
handoff_to_user_required).

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
      "app": "<app_id from the catalog>",
      "capability": "<capability id that exists on that app>",
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
5. Prefer the user's own wording in prompts; expand only to fill obvious gaps.
   Bake concrete values from the request directly into the prompts.
6. A single-app request is fine — emit a one-step plan.
7. If NO combination of the available apps/capabilities can satisfy the request,
   return instead: {"unsatisfiable": true, "reason": "<short explanation>"}.

No prose outside the fence."""


class FlowPlanner:
    def __init__(self, catalog: dict[str, Any], llm: OpenAI, model: str) -> None:
        self.catalog = catalog
        self._llm = llm
        self._model = model
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
        import json

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

        errors = self._validate(data)
        if errors:
            # TODO(repair): feed `errors` back to the LLM and retry (max ~2
            # rounds) before giving up. Deferred by design — for now we
            # hard-fail so a malformed plan never silently executes.
            self._repair(data, errors)  # no-op stub; see below
            raise PlanValidationError(nl_request, data, errors)

        n_legs = sum(1 for s in data["steps"] if not _is_ask_user(s))
        if n_legs >= 4:
            # No hard cap (by decision), but a long plan is worth flagging.
            logger.warning(f"synthesized plan has {n_legs} app legs — review the preview carefully")
        logger.info(f"planner: {len(data['steps'])} steps ({n_legs} app legs)")
        return data

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
        # (matches the hand-written xhs_to_amap_place flow ending on hail_ride).
        cap_meta = self._caps.get(app, {}).get(cap, {}) if app else {}
        if cap_meta.get("handoff_to_user_required") and idx != len(steps) - 1:
            nxt = steps[idx + 1]
            if not (isinstance(nxt, dict) and _is_ask_user(nxt)):
                errors.append(
                    f"step {sid!r}: capability {cap!r} is handoff_to_user_required and is "
                    f"not the final step — it must be followed by an ask_user step"
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
