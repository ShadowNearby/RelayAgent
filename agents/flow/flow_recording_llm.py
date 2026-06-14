"""Flow-process LLM-call recording.

A thin proxy over the OpenAI client that records every
`chat.completions.create` call (sanitized) so `FlowRunner` can fold each leg's
slice into the leg's traj.json under the top-level `flow_llm_calls` key — making
flow-process LLM cost (leg judge, bind extraction) observable alongside the
in-app agent's `["0"]["llm_calls"]`. Split out of `flow_runner.py`.
"""

from __future__ import annotations

import time
from typing import Any

from agents.llm.llm_retry import create_with_retry


def _sanitize_flow_messages(messages: list[dict]) -> list[dict]:
    """Strip giant base64 image_url payloads (leg-judge screenshots) so the
    folded traj.json stays readable; text content is left untouched."""
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        parts: list[Any] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if isinstance(url, str) and url.startswith("data:"):
                    parts.append({"type": "image_url", "image_url": {
                        "url": f"<base64 image, {len(url)} chars>"
                    }})
                    continue
            parts.append(part)
        out.append({**msg, "content": parts})
    return out


class _RecordingLLM:
    """Thin proxy over the OpenAI client that records every
    `chat.completions.create` call (sanitized) into `self.calls`. FlowRunner
    folds each leg's slice of that buffer into the leg's traj.json under the
    top-level `flow_llm_calls` key, so flow-process LLM cost (leg judge, bind
    extraction) is observable alongside the in-app agent's
    `["0"]["llm_calls"]`. The real response object is returned untouched.

    `purpose` is a caller-set label (e.g. "leg_judge", "bind_extract") stamped
    onto each recorded call; single-threaded flow so a plain attribute is enough.

    `retry=True` (FlowRunner's callers invoke `.chat.completions.create`
    directly, so the recorder owns the retry). Set `retry=False` when the
    caller already wraps the proxy in `create_with_retry` (e.g. the planner /
    capability router), so the gateway isn't retried twice over.
    """

    def __init__(self, client: Any, retry: bool = True) -> None:
        self._client = client
        self._retry = retry
        self.calls: list[dict] = []
        self.purpose = "flow"
        self.chat = _RecChat(self)


class _RecChat:
    def __init__(self, rec: _RecordingLLM) -> None:
        self.completions = _RecCompletions(rec)


class _RecCompletions:
    def __init__(self, rec: _RecordingLLM) -> None:
        self._rec = rec

    def create(self, *args: Any, **kwargs: Any) -> Any:
        rec = self._rec
        started = time.monotonic()
        record: dict[str, Any] = {
            "ts": time.time(),
            "purpose": rec.purpose,
            "model": kwargs.get("model"),
            "messages": _sanitize_flow_messages(kwargs.get("messages", [])),
            "kwargs": {k: kwargs[k] for k in ("temperature", "max_tokens")
                       if k in kwargs},
        }
        try:
            # Retry transient gateway failures (timeout/5xx/rate-limit) before
            # giving up — one flaky call shouldn't sink the whole flow leg.
            # Skip when the caller already retries (rec._retry=False) so we
            # don't nest create_with_retry over itself.
            resp = (
                create_with_retry(rec._client, *args, **kwargs)
                if rec._retry
                else rec._client.chat.completions.create(*args, **kwargs)
            )
        except Exception as e:  # best-effort logging — record then re-raise
            record["elapsed_s"] = round(time.monotonic() - started, 3)
            record["response"] = None
            record["error"] = repr(e)
            rec.calls.append(record)
            raise
        record["elapsed_s"] = round(time.monotonic() - started, 3)
        msg = resp.choices[0].message if getattr(resp, "choices", None) else None
        # qwen can null `content` and put the answer in `reasoning_content`.
        record["response"] = (
            (getattr(msg, "content", None) or getattr(msg, "reasoning_content", None))
            if msg is not None else None
        )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            record["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        rec.calls.append(record)
        return resp
