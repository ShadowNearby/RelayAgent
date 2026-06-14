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
import time
from typing import Any

from loguru import logger


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
    (`base_delay * 2**n` + jitter, capped at `max_delay`). Returns the raw
    response object on success; re-raises the last exception once attempts are
    exhausted. Surfaces each failed attempt at warning level (never debug) so
    transient gateway flakiness stays visible.
    """
    attempt = 0
    while True:
        try:
            return client.chat.completions.create(*args, **kwargs)
        except Exception as e:
            attempt += 1
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
