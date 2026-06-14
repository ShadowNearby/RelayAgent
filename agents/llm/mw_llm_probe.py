"""Non-invasive per-LLM-call probe for MobileWorld baseline runs.

MobileWorld's ``TrajLogger`` only persists an aggregate ``token_usage`` for the
whole run; it records no per-call latency and no per-call prompt/completion split.
This module monkeypatches MobileWorld's single LLM chokepoint —
``mobile_world.agents.base.BaseAgent.openai_chat_completions_create`` — to time
each call and capture its prompt/completion/cached token *delta*, then writes the
per-call records to the path named by ``RELAY_MW_LLM_CALLS_OUT`` at process exit.

It changes **no** decision logic: the wrapper invokes the original method
unchanged and only observes around it. Token deltas are read off the agent's own
``_total_*`` counters (which the original ``_log_openai_usage`` increments), so a
failed-then-retried call contributes nothing until it succeeds — exactly what the
aggregate ``token_usage`` already counts.

Activation is via a ``sitecustomize`` shim (``scripts/_mw_probe``) that
RelayAgent's mw driver puts on ``PYTHONPATH`` for the ``mw test`` subprocess, so
this never touches MobileWorld's installed source and survives ``uv sync``.

Streaming caveat: the streaming path returns a generator before usage is known, so
its per-call token delta reads as 0. ``general_e2e`` (the baseline agent type) does
not stream, so this does not affect the benchmark.
"""
from __future__ import annotations

import atexit
import json
import os
import time
from pathlib import Path
from typing import Any

_CALLS: list[dict[str, Any]] = []
_INSTALLED = False


def _sanitize_messages(messages: Any) -> Any:
    """Drop image bytes (keep a short placeholder) so the log stays readable and
    small, mirroring RelayAgent's own per-call message sanitization."""
    if not isinstance(messages, list):
        return messages
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        content = m.get("content")
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") in ("image_url", "image"):
                    parts.append({"type": "image", "image": "<image omitted>"})
                else:
                    parts.append(p)
            out.append({**m, "content": parts})
        else:
            out.append(m)
    return out


def _snapshot(agent: Any) -> tuple[int, int, int]:
    """Current (prompt, completion, cached) cumulative token counters of an agent."""
    return (
        getattr(agent, "_total_prompt_tokens", 0) or 0,
        getattr(agent, "_total_completion_tokens", 0) or 0,
        getattr(agent, "_total_cached_tokens", 0) or 0,
    )


def install() -> bool:
    """Patch MobileWorld's LLM chokepoint. Idempotent; inert without the OUT env.

    Returns True when the patch is in place, False when skipped (no out path or
    MobileWorld not importable in this process).
    """
    global _INSTALLED
    if _INSTALLED:
        return True
    out = os.getenv("RELAY_MW_LLM_CALLS_OUT")
    if not out:
        return False
    try:
        from mobile_world.agents.base import BaseAgent
    except Exception:  # MobileWorld not importable here — nothing to probe
        return False
    if getattr(BaseAgent.openai_chat_completions_create, "_relay_probe", False):
        _INSTALLED = True
        return True

    original = BaseAgent.openai_chat_completions_create

    def _wrapped(self: Any, *args: Any, **kwargs: Any):
        model = kwargs.get("model")
        if model is None and args:
            model = args[0]
        messages = kwargs.get("messages")
        if messages is None and len(args) >= 2:
            messages = args[1]
        before = _snapshot(self)
        t0 = time.perf_counter()
        ok = True
        resp: Any = None
        try:
            resp = original(self, *args, **kwargs)
            return resp
        except Exception:
            ok = False
            raise
        finally:
            elapsed = round(time.perf_counter() - t0, 4)
            after = _snapshot(self)
            prompt = after[0] - before[0]
            completion = after[1] - before[1]
            cached = after[2] - before[2]
            # response text (the chokepoint returns the model's string reply, like
            # RelayAgent's wrapper). Best-effort: keep it small/serializable.
            response: Any = resp
            if not isinstance(resp, (str, type(None))):
                response = str(resp)[:2000]
            _CALLS.append({
                "index": len(_CALLS),
                "purpose": type(self).__name__,
                "model": model,
                "elapsed_s": elapsed,
                "ok": ok,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cached_tokens": cached,
                "total_tokens": prompt + completion,
                "messages": _sanitize_messages(messages),
                "response": response,
            })

    _wrapped.__name__ = original.__name__
    _wrapped.__doc__ = original.__doc__
    _wrapped._relay_probe = True  # type: ignore[attr-defined]
    BaseAgent.openai_chat_completions_create = _wrapped  # type: ignore[assignment]
    atexit.register(_flush, Path(out))
    _INSTALLED = True
    return True


def _flush(out: Path) -> None:
    """Write the accumulated per-call records to ``out`` (best-effort)."""
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        prompt = sum(c["prompt_tokens"] for c in _CALLS)
        completion = sum(c["completion_tokens"] for c in _CALLS)
        cached = sum(c["cached_tokens"] for c in _CALLS)
        doc = {
            "n_calls": len(_CALLS),
            "total": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cached_tokens": cached,
                "total_tokens": prompt + completion,
            },
            "llm_calls": _CALLS,
        }
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
