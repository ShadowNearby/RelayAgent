"""VLM-based leg outcome judge.

A *leg* is one native-runner sub-run pinned to one app + one capability. The
hard signals in `flow_runner` (subprocess crash / empty reply / non-terminal
state) only catch *overt* failures — they cannot tell a confidently-wrong
answer from a correct one.

This mirrors MobileWorld's `BaseTask.is_successful` contract
(`-> (score, reason)`, 1.0 = success / 0.0 = failure), but RelayAgent runs over
open-world apps with no per-task ground-truth oracle, so instead of querying
controller/DB state we ask a VLM to read the leg's goal, the captured reply, and
the final on-screen state.

The leg's last step frame is a *pre-action* observation, so a leg whose final
action kicks off an async transition (e.g. live_navigation tapping a CTA) can
show a still-loading screen. Rather than papering over that by waiting and
re-capturing, we let the VLM name it: it classifies the final state into one of
three — **loading** (still in progress, outcome undetermined), **success**, or
**failure**. "loading" is NOT counted as a failure.

Best-effort by construction: any error (no frames, LLM down, unparseable
output) returns an *unknown* verdict (`judged=False`). Callers MUST NOT let a
judge failure abort the flow — surface it (per CLAUDE.md fallback policy) and
move on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from agents._img import pil_to_base64
from agents.action_model import ASK_USER

# Verdict states.
SUCCESS = "success"
FAILURE = "failure"
LOADING = "loading"        # still in progress — outcome not yet determined
UNKNOWN = "unknown"        # judge could not run / unparseable
_VALID = {SUCCESS, FAILURE, LOADING}

_BASE = (
    "You evaluate how a phone assistant's attempt at ONE delegated subtask turned "
    "out. You are given the subtask's goal, the assistant's final text reply (if "
    "any), and screenshot(s) of the final on-screen state.\n"
)

# `success` definition differs by leg kind; `loading`/`failure` are shared.
_SUCCESS_OUTCOME = (
    "the goal is actually accomplished — the requested action was carried out or "
    "the requested information is shown"
)
_SUCCESS_HANDOFF = (
    "the assistant correctly handed control back to the user — it surfaced the "
    "information the goal needs and the question/results it presents are on-topic "
    "and well-formed (deferring the final choice or confirmation to the user is "
    "EXPECTED here, not a failure)"
)


def _system(handoff: bool) -> str:
    success_def = _SUCCESS_HANDOFF if handoff else _SUCCESS_OUTCOME
    return (
        _BASE
        + "Classify the final state into EXACTLY one of:\n"
        + '- "loading": the app is still working — a spinner, progress bar, '
          "ellipsis, skeleton placeholder, blank/partial screen, or a transition "
          "is in flight. The outcome is NOT yet determined; do not guess success "
          "or failure.\n"
        + f'- "success": {success_def}.\n'
        + '- "failure": the app has FINISHED (no longer loading) but the goal was '
          "NOT met — an error or empty result, wrong/off-goal content, a "
          "login/permission wall, or it answers a different question than asked.\n"
        + 'Favor "loading" over a guess when the screen is clearly still in '
          "progress.\n"
        + "Output ONE JSON object inside a ```json``` fence, no prose outside it:\n"
        + '{"status": "loading"|"success"|"failure", "reason": "<one concise sentence>"}'
    )


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.+?\})\s*```", re.DOTALL)
_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)
_STEP_NUM_RE = re.compile(r"step_(\d+)\.png$")
# MobileWorld's TrajLogger names frames `<task>-0-<step>.png` under
# user_task/screenshots/ — used as a fallback for MobileWorld fallback legs.
_MW_STEP_NUM_RE = re.compile(r"-(\d+)\.png$")


@dataclass(frozen=True)
class LegVerdict:
    """Outcome of judging one leg.

    `status` is the primary field (`success`/`failure`/`loading`/`unknown`).
    `score` derives MobileWorld's convention (1.0 success / 0.0 failure; -1.0 for
    loading/unknown, i.e. no conclusive outcome)."""

    status: str
    reason: str

    @property
    def score(self) -> float:
        return {SUCCESS: 1.0, FAILURE: 0.0}.get(self.status, -1.0)

    @property
    def success(self) -> bool:
        return self.status == SUCCESS

    @property
    def judged(self) -> bool:
        """True only for a conclusive outcome — `loading` and `unknown` are not."""
        return self.status in (SUCCESS, FAILURE)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "score": self.score, "reason": self.reason}


def final_frames(leg_dir: Path, n: int = 2) -> list[Path]:
    """The last `n` step screenshots of a finished leg, oldest→newest.

    Reads the per-step PNGs the StepLogger drops under `<leg>/steps/`
    (see CLAUDE.md "Step 日志"). Sending the last two (not just one) lets the
    judge tell a stuck/loading screen apart from a settled final state.

    Falls back to MobileWorld's layout (`<leg>/user_task/screenshots/*.png`) when
    `steps/` is absent, so a MobileWorld fallback leg can also be judged."""
    steps_dir = leg_dir / "steps"
    if steps_dir.is_dir():
        def _num(p: Path) -> int:
            m = _STEP_NUM_RE.search(p.name)
            return int(m.group(1)) if m else -1

        # step_<n>.png only (skip step_<n>_marked.png — annotated dup).
        frames = sorted(
            (p for p in steps_dir.glob("step_*.png") if "_marked" not in p.name),
            key=_num,
        )
        return frames[-n:] if n > 0 else frames

    mw_dir = leg_dir / "user_task" / "screenshots"
    if mw_dir.is_dir():
        def _mw_num(p: Path) -> int:
            m = _MW_STEP_NUM_RE.search(p.name)
            return int(m.group(1)) if m else -1

        frames = sorted(
            (p for p in mw_dir.glob("*.png") if "marked" not in p.name),
            key=_mw_num,
        )
        return frames[-n:] if n > 0 else frames

    return []


def judge_leg(
    *,
    llm,
    model: str,
    goal: str,
    app: str,
    capability: str,
    reply: str,
    frames: list[Path],
    live_image: Any = None,
    terminal_action: str | None = None,
    max_tokens: int = 1024,
) -> LegVerdict:
    """Classify a finished leg as success / failure / loading. Never raises.

    `frames` are per-step PNGs (pre-action observations). `live_image`, when
    given (a PIL.Image), is a fresh screenshot of the *current* device state —
    used by the caller's loading-retry path: a leg the judge first calls
    `loading` is re-judged against a freshly captured frame so a screen that has
    since settled is reclassified. It is sent last and called out as the current
    screen.

    `terminal_action` is the leg's last action type (from the sub-run summary).
    When it is `ask_user` the leg is a handoff — the assistant is meant to defer
    the final decision to the user, so the `success` definition changes (see
    `_SUCCESS_HANDOFF`)."""
    if not frames and live_image is None:
        logger.info(f"leg judge skipped for {app}/{capability}: no final frames")
        return LegVerdict(UNKNOWN, "no frames to judge")

    handoff = terminal_action == ASK_USER
    ask = (
        "Did the assistant hand off to the user correctly, or is it still loading?"
        if handoff
        else "Did the assistant accomplish the goal, or is it still loading?"
    )
    live_note = (
        " The LAST image is the CURRENT screen, captured just now — judge the "
        "state from it."
        if live_image is not None
        else ""
    )
    parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"App: {app}\nCapability: {capability}\n"
                f"Subtask goal:\n{goal}\n\n"
                f"Assistant final reply:\n{reply.strip() or '(no text reply captured)'}\n\n"
                f"The screen(s) below are the latest, in order.{live_note} {ask}"
            ),
        }
    ]

    def _add_image(img: Any) -> None:
        b64 = pil_to_base64(img)  # accepts PIL.Image or raw PNG bytes
        parts.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
        )

    for fp in frames:
        try:
            _add_image(fp.read_bytes())
        except OSError as e:  # missing/unreadable frame — judge on what's left
            logger.warning(f"leg judge could not read frame {fp.name}: {e}")
    if live_image is not None:
        _add_image(live_image)
    if len(parts) == 1:  # text only — every frame failed to load
        return LegVerdict(UNKNOWN, "no readable frames")

    try:
        resp = llm.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _system(handoff)},
                {"role": "user", "content": parts},
            ],
            temperature=0.0,
            max_tokens=max_tokens,
        )
    except Exception as e:  # network / endpoint — best-effort, don't escalate
        logger.warning(f"leg judge LLM call failed for {app}/{capability}: {e}")
        return LegVerdict(UNKNOWN, f"judge call failed: {e}")

    msg = resp.choices[0].message
    raw = (msg.content or "").strip()
    # qwen can return a null content with the answer in reasoning_content.
    if not raw and "qwen" in model.lower():
        raw = (getattr(msg, "reasoning_content", None) or "").strip()

    parsed = _parse_verdict(raw)
    if parsed is None:
        logger.warning(f"leg judge returned unparseable output for {app}/{capability}: {raw!r}")
        return LegVerdict(UNKNOWN, "unparseable judge output")

    status, reason = parsed
    kind = "handoff" if handoff else "outcome"
    logger.info(f"leg judge {app}/{capability} [{kind}]: {status.upper()} — {reason}")
    return LegVerdict(status, reason)


def _parse_verdict(raw: str) -> tuple[str, str] | None:
    """Pull `{status, reason}` out of the model's reply; None if unrecoverable.

    Tolerates a legacy `{"success": bool}` shape by mapping it to status."""
    if not raw:
        return None
    m = _FENCE_RE.search(raw) or _OBJ_RE.search(raw)
    payload = m.group(1) if (m and m.re is _FENCE_RE) else (m.group(0) if m else raw)
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return _salvage_verdict(raw)
    if not isinstance(data, dict):
        return None
    reason = str(data.get("reason", "")).strip()
    status = str(data.get("status", "")).strip().lower()
    if status in _VALID:
        return status, reason
    if "success" in data:  # legacy boolean shape
        return (SUCCESS if bool(data["success"]) else FAILURE), reason
    return None


_STATUS_SALVAGE_RE = re.compile(r'"status"\s*:\s*"(success|failure|loading)"')
_REASON_SALVAGE_RE = re.compile(r'"reason"\s*:\s*"([^"]*)')


def _salvage_verdict(raw: str) -> tuple[str, str] | None:
    """JSON 不可解析（典型：max_tokens 把 ```json 块截断在字符串中间）时，
    直接从残文里捞 status；reason 尽量带上截断前缀。"""
    m = _STATUS_SALVAGE_RE.search(raw)
    if m is None:
        return None
    rm = _REASON_SALVAGE_RE.search(raw)
    reason = (rm.group(1).strip() if rm else "") or "(salvaged from truncated judge output)"
    return m.group(1), reason
