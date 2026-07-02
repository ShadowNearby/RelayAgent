"""Plan-shape + prompt-template helpers for the flow planner.

Pure functions split out of `flow_planner.py`: step-kind predicates, `{var}`
root extraction, and the capability `prompt_template` filling (including the
optional `[ ... {slot} ... ]` segment handling). `_VAR_RE` / `MW_STEP_TYPE` are
shared with the runner via `flow_runner_util`.
"""

from __future__ import annotations

import re

from agents.flow.flow_runner_util import MW_STEP_TYPE, _VAR_RE, _select_from_path


def _is_ask_user(step: dict) -> bool:
    return isinstance(step, dict) and step.get("type") == "ask_user"


def _is_mw_leg(step: dict) -> bool:
    return isinstance(step, dict) and step.get("type") == MW_STEP_TYPE


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
            sel = step.get("select_from")
            # `{var}` / `var.field` spellings resolve to the root bind name,
            # via the same helper the validator and the runner use.
            if isinstance(sel, str) and (_select_from_path(sel) or [None])[0] == bind:
                return True
            fields = [step.get("prompt_header", "")]
        else:
            fields = [step.get("prompt", "")]
            if isinstance(step.get("extract"), dict):
                fields.append(step["extract"].get("prompt", ""))
        if any(bind in _var_roots(str(field or "")) for field in fields):
            return True
    return False
