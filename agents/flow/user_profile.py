"""User memory layer (roadmap P3): a local, explicit, inspectable profile.

The project's premise is that task context already lives inside the apps; this
layer only fills in preferences the user didn't spell out ("send it home",
"the usual") — no scraping, no silent writes.

Store: ``${RELAY_PROFILE_ROOT:-~/.relayagent}/profile.yaml`` (Android points
RELAY_PROFILE_ROOT at filesDir, same pattern as RELAY_TRAJ_ROOT). Schema in
``spec/profile.schema.json``; the loader also validates structurally so a
malformed file degrades to "no profile" with a warning, never a crash.

Sections (all optional, all flat ``str → str`` maps except noted):

- ``addresses``    — named places ("home", "company", …)
- ``contacts``     — alias → who is meant ("老板" → "张三")
- ``preferences``  — stable tastes ("milk_tea" → "去冰三分糖")
- ``app_hints``    — app_id → free-text hint injected for that app's legs
- ``last_choices`` — ask_user select_from memory (question key → chosen label);
  written automatically on every choice: it records the user's OWN explicit
  pick, not an inference (M3's never-write-silently rule covers inferred
  preferences only)

Knobs: ``RELAY_PROFILE`` (default 1; 0 = layer fully off, today's behavior —
also the no-file behavior) / ``RELAY_PROFILE_ROOT`` (store dir) /
``RELAY_TRAJ_REDACT`` (default 0; 1 = profile VALUES are replaced by
``<profile:section.key>`` placeholders in trajectory logs — M4).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

_SECTIONS = ("addresses", "contacts", "preferences", "app_hints", "last_choices")
# Sections whose values are injected into prompts / redacted from trajs.
# last_choices is bookkeeping (labels already appear on screen), not PII-bearing
# by design, but redact it too — labels can be addresses.
_VALUE_SECTIONS = ("addresses", "contacts", "preferences", "app_hints", "last_choices")


def profile_enabled() -> bool:
    return os.getenv("RELAY_PROFILE", "1") == "1"


def profile_path() -> Path:
    root = os.getenv("RELAY_PROFILE_ROOT") or "~/.relayagent"
    return Path(root).expanduser() / "profile.yaml"


class UserProfile:
    """One loaded profile.yaml. Mutations go through helpers + save() (atomic
    temp+rename) so a crash never leaves a half-written store."""

    def __init__(
        self,
        data: dict[str, Any],
        path: Path,
        invalid_sections: set[str] | None = None,
    ) -> None:
        self.data = data
        self.path = path
        # Sections load_profile found malformed: ignored on READ, kept intact
        # in `data` (save() writes it verbatim, so the user's original values
        # survive on disk) and refused as WRITE targets — a degraded read must
        # never turn into a destructive write-back.
        self._invalid_sections: set[str] = set(invalid_sections or ())

    # --------------------------------------------------------------- access

    def section(self, name: str) -> dict[str, str]:
        if name in self._invalid_sections:
            return {}
        sec = self.data.get(name)
        return sec if isinstance(sec, dict) else {}

    def _writable(self, name: str) -> bool:
        if name in self._invalid_sections:
            logger.warning(
                f"profile section {name!r} is malformed on disk; skipping the "
                f"write so the original data is preserved"
            )
            return False
        return True

    def is_empty(self) -> bool:
        return not any(self.section(s) for s in ("addresses", "contacts", "preferences", "app_hints"))

    def summary(self) -> str:
        """Compact text block for prompt injection (M2). Only the four
        user-facing sections — never last_choices."""
        parts: list[str] = []
        for sec, title in (
            ("addresses", "known addresses"),
            ("contacts", "contact aliases"),
            ("preferences", "stable preferences"),
            ("app_hints", "per-app hints"),
        ):
            entries = self.section(sec)
            if entries:
                body = "; ".join(f"{k}={v}" for k, v in entries.items())
                parts.append(f"{title}: {body}")
        return "\n".join(parts)

    def flat_values(self) -> dict[str, str]:
        """``section.key → value`` for every redactable value (M4)."""
        out: dict[str, str] = {}
        for sec in _VALUE_SECTIONS:
            for k, v in self.section(sec).items():
                if isinstance(v, str) and v.strip():
                    out[f"{sec}.{k}"] = v
        return out

    # ----------------------------------------------------- choice memory (M2③)

    def get_choice(self, question_key: str) -> str | None:
        return self.section("last_choices").get(_choice_key(question_key)) or None

    def remember_choice(self, question_key: str, label: str) -> None:
        if not label or not self._writable("last_choices"):
            return
        self.data.setdefault("last_choices", {})[_choice_key(question_key)] = label
        self.save()

    # -------------------------------------------------------- writes (M3)

    def add_preference(self, key: str, value: str) -> None:
        if not self._writable("preferences"):
            return
        self.data.setdefault("preferences", {})[key] = value
        self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".yaml")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(self.data, fh, allow_unicode=True, sort_keys=False)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning(f"profile save failed: {e}")


def _choice_key(question_key: str) -> str:
    return " ".join(str(question_key).split())[:120]


def load_profile() -> UserProfile | None:
    """The user profile, or None (disabled / absent / malformed → warned).

    Loaded fresh per call — the file is tiny and callers sit at flow
    boundaries, not in the step loop.
    """
    if not profile_enabled():
        return None
    path = profile_path()
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning(f"profile at {path} unreadable ({e}); running without it")
        return None
    if not isinstance(data, dict):
        logger.warning(f"profile at {path} is not a mapping; running without it")
        return None
    invalid: set[str] = set()
    for sec in _SECTIONS:
        val = data.get(sec)
        if val is None:
            continue
        if not isinstance(val, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in val.items()
        ):
            # Only FLAG the section (ignored on read, write-protected) — do
            # not blank it in `data`, or the next save() would persist the
            # loss and silently destroy the user's hand-edited values.
            logger.warning(
                f"profile section {sec!r} must be a flat string→string map; ignoring it"
            )
            invalid.add(sec)
    return UserProfile(data, path, invalid)


# ------------------------------------------------------------- redaction (M4)

def redact_enabled() -> bool:
    return os.getenv("RELAY_TRAJ_REDACT", "0") == "1"


def redact_obj(obj: Any) -> Any:
    """Deep-copy `obj` with every profile VALUE replaced by a
    ``<profile:section.key>`` placeholder. No-op (same object) when redaction
    is off or there is no profile — the hot path stays free.

    Applied at trajectory WRITE time (llm-call logs, step logs, flow reports):
    prompts carry profile values by design; sharing a traj for debugging must
    not leak a home address.
    """
    if not redact_enabled():
        return obj
    profile = load_profile()
    if profile is None:
        return obj
    values = sorted(profile.flat_values().items(), key=lambda kv: -len(kv[1]))
    if not values:
        return obj

    def _walk(x: Any) -> Any:
        if isinstance(x, str):
            for key, val in values:
                if val in x:
                    x = x.replace(val, f"<profile:{key}>")
            return x
        if isinstance(x, dict):
            return {k: _walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [_walk(v) for v in x]
        return x

    return _walk(obj)


# ------------------------------------------------------- memory writes (M3)

_MEMORY_SYSTEM = (
    "You watch one completed phone-automation task and decide whether it "
    "revealed ONE stable, reusable user preference worth remembering for "
    "future tasks (a dietary preference, a named address, a habitual choice). "
    "Task-specific facts (a one-off search result, an order number, a date) "
    "are NOT preferences. Reply with ONLY JSON: "
    '{"save": true, "key": "<short_snake_case>", "value": "<the preference>"} '
    'or {"save": false}. The key/value language should match the user\'s.'
)


def propose_memory(llm: Any, model: str, request: str, blackboard: dict[str, Any]) -> tuple[str, str] | None:
    """One cheap LLM call: did this flow surface a stable preference?

    Returns (key, value) to PROPOSE to the user, or None. The caller must ask
    y/n before writing — never write silently (M3).
    """
    bb_view = {k: str(v)[:200] for k, v in blackboard.items()}
    user = json.dumps(
        {"user_request": request, "task_results": bb_view}, ensure_ascii=False
    )
    if hasattr(llm, "purpose"):
        llm.purpose = "memory_propose"
    try:
        resp = llm.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _MEMORY_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=128,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001 — memory is best-effort, never fatal
        logger.info(f"memory proposal skipped ({e})")
        return None
    if not isinstance(data, dict) or not data.get("save"):
        return None
    key, value = str(data.get("key") or "").strip(), str(data.get("value") or "").strip()
    if not key or not value:
        return None
    return key, value
