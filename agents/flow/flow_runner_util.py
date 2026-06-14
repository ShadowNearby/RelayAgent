"""Shared constants + small pure helpers for the flow runner.

Split out of `flow_runner.py` so the templating (`{var}` substitution), fenced
JSON parsing, MobileWorld traj harvesting, terminal-state assertion and choice
resolution live next to — but separate from — the `FlowRunner` class. The
`FlowPlanner` also imports `_VAR_RE` / `_parse_fenced_json` from here so it
validates exactly what the runner later templates against.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

from loguru import logger

from agents.agent.action_model import ANSWER, ASK_USER, FINISHED

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = REPO_ROOT / ".env"
# Each app leg is a fresh native runner subprocess (direct adb).
NATIVE_RUNNER_MODULE = "agents.runtime.native_runner"
# MobileWorld fallback legs shell out to this driver (manages the MW server,
# prelaunch, .env LLM config) — see scripts/run_mobileworld.py.
RUN_MOBILEWORLD = REPO_ROOT / "scripts" / "run_mobileworld.py"
MW_STEP_TYPE = "mobileworld"


# --------------------------------------------------------------------------- #
# templating
# --------------------------------------------------------------------------- #

_VAR_RE = re.compile(r"\{([a-zA-Z_][\w.]*)\}")


def render(template: str, ctx: dict[str, Any]) -> str:
    """Substitute `{var}` and `{var.field}` against ctx. Missing keys → ''."""
    def repl(m: re.Match) -> str:
        path = m.group(1).split(".")
        v: Any = ctx
        for p in path:
            if isinstance(v, dict):
                v = v.get(p, "")
            else:
                v = getattr(v, p, "")
        return "" if v is None else str(v)
    return _VAR_RE.sub(repl, template)


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _parse_fenced_json(text: str) -> Any:
    m = _FENCE_RE.search(text)
    payload = m.group(1) if m else text
    # strict=False tolerates raw control characters (literal newlines/tabs) inside
    # string values, which some models emit instead of escaping them.
    return json.loads(payload, strict=False)


def _redact(d: dict[str, Any]) -> dict[str, Any]:
    """Shallow redact obvious secrets in blackboard logging."""
    out = {}
    for k, v in d.items():
        if "key" in k.lower() or "token" in k.lower():
            out[k] = "***"
        else:
            out[k] = v
    return out


def _load_mw_driver():
    """Load scripts/run_mobileworld.py as a module (server health/start/wait
    helpers live there; reuse rather than duplicate)."""
    path = REPO_ROOT / "scripts" / "run_mobileworld.py"
    spec = importlib.util.spec_from_file_location("run_mobileworld", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _harvest_mw_traj(leg_dir: Path) -> tuple[str, str | None, str | None]:
    """Pull (reply, terminal_action_type, goal_status) from a MobileWorld leg.

    MobileWorld's TrajLogger writes <leg_dir>/user_task/traj.json shaped like
    `{"0": {"traj": [{"action": {...}}, ...]}}`. The leg reply is the text of the
    last `answer` action; the terminal signal is the last action overall.
    Best-effort — a missing/garbled traj yields ("", None, None)."""
    traj = _read_json_file(leg_dir / "user_task" / "traj.json")
    node = traj.get("0") if isinstance(traj.get("0"), dict) else {}
    steps = node.get("traj") if isinstance(node.get("traj"), list) else []
    if not steps:
        return "", None, None
    last_action = (steps[-1].get("action") or {}) if isinstance(steps[-1], dict) else {}
    terminal_action = last_action.get("action_type")
    goal_status = last_action.get("goal_status")
    reply = ""
    for entry in reversed(steps):
        action = (entry.get("action") or {}) if isinstance(entry, dict) else {}
        if action.get("action_type") == ANSWER and (action.get("text") or "").strip():
            reply = action["text"].strip()
            break
    return reply, terminal_action, goal_status


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.exists() and path.stat().st_size > 0:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"failed to read native summary {path}: {exc}")
    return {}


def _assert_output_free_step_completed(
    step: dict,
    summary: dict[str, Any],
    rc: int,
    summary_path: Path,
) -> None:
    """No-reply legs still need a positive terminal signal from the child run."""
    last_action = summary.get("last_action_type")
    goal_status = summary.get("last_goal_status")
    ok = (
        rc == 0
        and (
            last_action in {ASK_USER, ANSWER}
            or (last_action == FINISHED and goal_status == "complete")
        )
    )
    if ok:
        return
    raise RuntimeError(
        f"Step {step['id']!r}: output-free native run did not reach a successful "
        f"terminal state (rc={rc}, last_action_type={last_action!r}, "
        f"last_goal_status={goal_status!r}). Check {summary_path.parent}."
    )


def _resolve_choice(raw: str, items: list[Any], label_tpl: str) -> Any:
    if not raw:
        return items[0]
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(items):
            return items[idx]
    # substring match against rendered label, then `name`
    lowered = raw.lower()
    for it in items:
        if lowered in render(label_tpl, it).lower():
            return it
    for it in items:
        if isinstance(it, dict) and lowered in str(it.get("name", "")).lower():
            return it
    raise ValueError(f"Could not resolve user choice {raw!r} among {len(items)} items")
