"""Shared retry wrapper for OpenAI chat-completions calls.

The flow-process LLM call sites — `flow_planner` (plan synthesis / routing),
`capability_matrix_router`, and the leg-judge / bind-extract calls routed
through `flow_runner._RecordingLLM` — bypass
`BaseAgent.openai_chat_completions_create` (which has its own retry loop) and
talk to the OpenAI client directly. A single transient gateway hiccup (timeout,
5xx, rate limit) there used to crash the whole flow. `create_with_retry` wraps
`client.chat.completions.create(...)` with bounded retries and exponential
backoff so those calls survive transient failures.

`client` may be a real `OpenAI` client or the flow's `_RecordingLLM` proxy —
both expose `.chat.completions.create`.
"""
from __future__ import annotations

import random
import re
import time
from typing import Any

from loguru import logger

# LLMHTTPError (the stdlib shim) encodes the status as an `HTTP {code}` message
# prefix — see agents/llm/llm_client.py.
_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})\b")


def _http_status(e: Exception) -> int | None:
    """HTTP status attached to an LLM-call failure, if any.

    openai SDK errors carry `.status_code`; the stdlib shim's LLMHTTPError
    carries it in the message prefix. None = no status (connection error,
    timeout, unknown exception)."""
    status = getattr(e, "status_code", None)
    if isinstance(status, int):
        return status
    m = _HTTP_STATUS_RE.match(str(e))
    return int(m.group(1)) if m else None


def _is_retryable(e: Exception) -> bool:
    """Whether a failure is plausibly transient (this module's stated scope).

    Deterministic failures — 4xx parameter/auth errors, a bad call signature —
    would just replay doomed requests against the gateway and bury the real
    config error under `retrying` warnings. Retry only request timeout (408),
    conflict (409), rate limit (429), and server errors (5xx); a status-less
    failure (connection error / timeout / unknown) is conservatively assumed
    transient."""
    if isinstance(e, TypeError):
        return False  # bad call signature — deterministic
    status = _http_status(e)
    if status is None:
        return True
    return status in (408, 409, 429) or status >= 500


def create_with_retry(
    client: Any,
    *args: Any,
    retry_times: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
    **kwargs: Any,
) -> Any:
    """Call `client.chat.completions.create(**kwargs)`, retrying on exception.

    Retries up to `retry_times` total attempts with exponential backoff
    (`base_delay * 2**n` + jitter, capped at `max_delay`). Deterministic
    failures (see `_is_retryable`) are re-raised immediately without retrying.
    Returns the raw response object on success; re-raises the last exception
    once attempts are exhausted. Surfaces each failed attempt at warning level
    (never debug) so transient gateway flakiness stays visible.
    """
    # A proxy that already retries internally must not be wrapped again:
    # flow_runner's _RecordingLLM(retry=True) routes its own create through
    # create_with_retry, so retrying it here would nest 3x3 attempts and
    # triple the backoff against an already-failing gateway (the recovery
    # reroute path reaches here with exactly that proxy). Such proxies
    # advertise it via a truthy `_retry` attribute (see flow_recording_llm).
    if getattr(client, "_retry", False):
        return client.chat.completions.create(*args, **kwargs)

    attempt = 0
    while True:
        try:
            return client.chat.completions.create(*args, **kwargs)
        except Exception as e:
            attempt += 1
            if not _is_retryable(e):
                logger.warning(
                    f"LLM call failed with a non-retryable error, giving up immediately: {e}"
                )
                raise
            if attempt >= retry_times:
                logger.warning(
                    f"LLM call failed after {attempt} attempt(s), giving up: {e}"
                )
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            delay += random.uniform(0, 0.3)
            logger.warning(
                f"LLM call failed (attempt {attempt}/{retry_times}), "
                f"retrying in {delay:.1f}s: {e}"
            )
            time.sleep(delay)
