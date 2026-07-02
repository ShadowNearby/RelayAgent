"""Shared app-card catalog helpers.

The catalog is a compact JSON-able digest of every manifest's embedded-agent
surface. It is used by both single-app routing and cross-app planning.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agents.routing.card_loader import load_all_cards

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST_DIR = REPO_ROOT / "manifests"

# Any `{placeholder}` token in an *authored* template. At manifest-authoring
# time a template may only reference declared slots; cross-step `{var}` tokens
# enter later as slot *values*, never as literal template text.
_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
# Optional segment `[ ... ]` (non-nested), mirrors flow_planner._OPT_SEGMENT_RE.
_OPT_SEGMENT_RE = re.compile(r"\[([^\[\]]*)\]")


class ManifestValidationError(ValueError):
    """Raised at catalog-build time when a `prompt_template` is malformed.

    Pulls the failure forward from runtime (where only the executed step would
    trip flow_planner's PromptTemplateError) to load time, so a typo'd
    placeholder or a mis-bracketed optional slot fails fast and loud.
    """


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _validate_prompt_template(
    app_id: str, cap_id: str, template: str, slot_specs: list[dict]
) -> list[str]:
    """Return a list of human-readable problems with one capability's template.

    Checks (fail-fast contract for `prompt_template` / `prompt_slots`):
    - brackets are balanced and non-nested;
    - every `{placeholder}` is a declared slot (catches typos like `{palce}`);
    - every declared slot is referenced at least once (no dead slots);
    - every REQUIRED slot appears OUTSIDE any optional segment (so it is never
      droppable);
    - every OPTIONAL slot appears ONLY inside `[...]` (so an empty value drops
      its surrounding wording instead of leaving a gap).
    """
    where = f"{app_id}/{cap_id}"
    errors: list[str] = []

    if template.count("[") != template.count("]") or re.search(r"\[[^\]]*\[", template):
        errors.append(f"{where}: unbalanced or nested '[]' in template {template!r}")
        return errors  # segment-dependent checks below would be unreliable

    slot_names = [s.get("name") for s in slot_specs if s.get("name")]
    required = {s["name"] for s in slot_specs if s.get("name") and s.get("required", True)}
    optional = {s["name"] for s in slot_specs if s.get("name") and not s.get("required", True)}

    in_segment: set[str] = set()
    for seg in _OPT_SEGMENT_RE.findall(template):
        in_segment.update(_PLACEHOLDER_RE.findall(seg))
    all_used = set(_PLACEHOLDER_RE.findall(template))
    outside_segment = all_used - in_segment

    for ph in sorted(all_used):
        if ph not in slot_names:
            errors.append(f"{where}: template references undeclared slot {{{ph}}}")
    for name in slot_names:
        if name not in all_used:
            errors.append(f"{where}: slot {name!r} declared but never used in template")
    for name in sorted(required & in_segment - outside_segment):
        errors.append(f"{where}: required slot {name!r} sits only inside a '[...]' segment (would be droppable)")
    for name in sorted(optional & outside_segment):
        errors.append(f"{where}: optional slot {name!r} must be wrapped in a '[...]' segment")
    return errors


def build_catalog(manifest_dir: Path = MANIFEST_DIR) -> dict[str, Any]:
    """Compact JSON-able view of available apps for router/planner LLMs."""
    apps: list[dict[str, Any]] = []
    errors: list[str] = []
    # Single manifest reader: load_all_cards handles `_`-prefix skipping,
    # app_id validation, and per-file error isolation. Every manifest consumer
    # routes through it, so a learned overlay merged there is seen everywhere
    # (routing/planning here, runtime execution in relay_agent) — not just on
    # one path.
    for doc in load_all_cards(manifest_dir):
        agent = doc.get("embedded_agent") or {}
        caps = []
        seen_cap_ids: set[str] = set()
        for c in agent.get("capabilities") or []:
            # SPEC §8: capability ids are unique within a card. Duplicates would
            # make build_plan's `next(c for c ...)` silently pick the first.
            cid = c.get("id") or "?"
            if cid in seen_cap_ids:
                errors.append(
                    f"{doc.get('app_id') or '?'}: duplicate capability id {cid!r}"
                )
            seen_cap_ids.add(cid)
            cap = {
                "id": c.get("id"),
                "description": clean_text(c.get("description")),
                "examples": c.get("example_prompts") or [],
                "executable": c.get("executable", True),
                "handoff_to_user_required": c.get("handoff_to_user_required", False),
                "x_skip_wait_for_reply": c.get("x_skip_wait_for_reply", False),
            }
            # Templated submit prompt (see flow_planner._fill_prompt_template).
            # Only carried through when present so the planner LLM's catalog
            # stays lean for capabilities that take a free-form prompt.
            if c.get("prompt_template"):
                cap["prompt_template"] = c["prompt_template"]
                cap["prompt_slots"] = c.get("prompt_slots") or []
                errors.extend(
                    _validate_prompt_template(
                        doc.get("app_id") or "?",
                        c.get("id") or "?",
                        c["prompt_template"],
                        cap["prompt_slots"],
                    )
                )
            caps.append(cap)
        apps.append({
            "app_id": doc.get("app_id"),
            "app_name": doc.get("app_name"),
            "locale": doc.get("locale") or [],
            "agent_name": agent.get("name"),
            "agent_description": clean_text(agent.get("description")),
            "capabilities": caps,
        })

    if errors:
        raise ManifestValidationError(
            "invalid manifest(s):\n  - " + "\n  - ".join(errors)
        )

    return {"apps": apps}
