"""LLM client factory — OpenAI SDK on host, stdlib-HTTP shim on Android.

Every flow/agent construction site obtains its chat-completions client via
`make_llm_client` instead of constructing `openai.OpenAI` directly. On host
the real SDK is returned (behavior-identical to before). When the SDK is not
importable (the Android/Chaquopy build doesn't ship it — pydantic-core has no
wheel) or `RELAY_LLM_HTTP=1` forces it, `HttpChatClient` speaks the same
`client.chat.completions.create(...)` surface over stdlib urllib.

The shim mirrors exactly the response surface our callers read (see
tests/test_llm_client.py):

- `resp.choices[0].message.content` / `.reasoning_content` (qwen thinking
  models put the answer there when content is null)
- `resp.usage.prompt_tokens / .completion_tokens / .total_tokens` and
  `resp.usage.prompt_tokens_details.cached_tokens`
  (`BaseAgent._log_openai_usage`, `flow_runner._RecCompletions`)
- errors raise with the HTTP status + response body in `str(e)`, so
  `BaseAgent.openai_chat_completions_create`'s max_tokens-retry sniffing and
  `llm_retry.create_with_retry` keep working.

No streaming: nothing in the runtime passes `stream=True` (verified across
agents/ + scripts/); the shim raises if someone starts to.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any

from loguru import logger

# Mirrors the OpenAI SDK's default request timeout, used when the caller
# doesn't pass one (flow_runner / run_plan construct without a timeout).
_SDK_DEFAULT_TIMEOUT = 600.0


class LLMHTTPError(RuntimeError):
    """Chat-completions HTTP failure. str() carries status + body excerpt."""


def make_llm_client(base_url: str, api_key: str | None, timeout: float | None = None):
    """Return a chat-completions client for `base_url`.

    Real `openai.OpenAI` when importable (host default — keeps SDK retry /
    connection-pool behavior untouched); `HttpChatClient` when the SDK is
    missing (Android) or `RELAY_LLM_HTTP=1` forces the shim (testing/parity).
    """
    key = api_key if api_key else "empty"
    if os.getenv("RELAY_LLM_HTTP") != "1":
        try:
            from openai import OpenAI

            if timeout is None:
                return OpenAI(base_url=base_url, api_key=key)
            return OpenAI(base_url=base_url, api_key=key, timeout=timeout)
        except ImportError:
            logger.info("openai SDK not importable; using stdlib HTTP chat client")
    return HttpChatClient(base_url, key, timeout=timeout)


class HttpChatClient:
    """`client.chat.completions.create(...)` over stdlib urllib."""

    def __init__(self, base_url: str, api_key: str, timeout: float | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout if timeout is not None else _SDK_DEFAULT_TIMEOUT
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: bool = False,
        extra_body: dict | None = None,
        **kwargs: Any,
    ) -> Any:
        if stream:
            raise NotImplementedError("HttpChatClient does not support stream=True")
        kwargs.pop("stream_options", None)  # only meaningful with stream
        body: dict[str, Any] = {"model": model, "messages": messages}
        body.update({k: v for k, v in kwargs.items() if v is not None})
        if extra_body:
            body.update(extra_body)

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:2000]
            except Exception:  # body read is best-effort
                pass
            raise LLMHTTPError(f"HTTP {e.code} from {self.base_url}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMHTTPError(f"chat/completions request failed: {e}") from e

        return _normalize_response(payload)


def _to_namespace(obj: Any) -> Any:
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_to_namespace(v) for v in obj]
    return obj


def _normalize_response(payload: dict) -> Any:
    """JSON → attribute-access response with the SDK-guaranteed fields present.

    The SDK's typed models always expose content / usage token counts even
    when the gateway omits them; consumers access those attributes directly,
    so default the ones they touch."""
    resp = _to_namespace(payload)
    if not getattr(resp, "choices", None):
        raise LLMHTTPError(f"chat/completions response has no choices: {payload}")
    for choice in resp.choices:
        msg = getattr(choice, "message", None)
        if msg is None:
            choice.message = SimpleNamespace(content=None)
        elif not hasattr(msg, "content"):
            msg.content = None
    usage = getattr(resp, "usage", None)
    if usage is None:
        resp.usage = None  # _log_openai_usage checks for None explicitly
    else:
        for f in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if not hasattr(usage, f):
                setattr(usage, f, 0)
    return resp
