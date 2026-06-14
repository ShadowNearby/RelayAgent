"""Agent base classes — OpenAI client + token accounting + lifecycle.

`BaseAgent` + `MCPAgent`, which RelayAgent subclasses. Provides
`build_openai_client`, `openai_chat_completions_create` (including the
claude/gpt/o1/kimi parameter quirks and the max_tokens→max_completion_tokens
retry), and the running-total token accounting RelayAgent reads via
`self._total_*` and `get_total_token_usage()`.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from loguru import logger

from agents.agent.action_model import JSONAction
from agents.llm.llm_client import make_llm_client


class BaseAgent(ABC):
    """Abstract base class for all mobile automation agents."""

    def __init__(self, *args: Any, **kwargs: Any):
        self._total_completion_tokens: int = 0
        self._total_prompt_tokens: int = 0
        self._total_cached_tokens: int = 0

    def initialize(self, instruction: str) -> bool:
        """Initialize the agent with the given instruction."""
        self.instruction = instruction
        logger.debug(f"initialized the agent with the given instruction: {self.instruction}")
        self.initialize_hook(self.instruction)
        return True

    def initialize_hook(self, instruction: str) -> None:
        """Hook for initializing the agent."""
        pass

    @abstractmethod
    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        """Generate the next action based on current observation."""
        raise NotImplementedError("predict method is not implemented")

    def done(self) -> None:
        """Finalize the agent for the current task."""
        logger.debug(f"finalizing the agent for the current task: {self.instruction}")
        self.instruction = None
        self.reset()

    def reset(self) -> None:
        """Reset the agent for the next task."""
        logger.warning(
            "reset method is not implemented, note the agent memory will be carried "
            "over to the next task"
        )

    def build_openai_client(self, base_url: str, api_key: str) -> None:
        """Build the chat-completions client (OpenAI SDK on host, stdlib HTTP
        shim when the SDK is unavailable — see agents.llm.llm_client)."""
        self.openai_client = make_llm_client(base_url, api_key, timeout=120.0)
        logger.debug(f"built the LLM client with base_url={base_url}")

    def _wrap_stream_with_usage_logging(self, stream: Any) -> Any:
        """Wrap a streaming response to log usage when the stream completes."""
        final_usage = None
        for chunk in stream:
            if hasattr(chunk, "usage") and chunk.usage is not None:
                final_usage = chunk
            yield chunk

        if final_usage is not None:
            self._log_openai_usage(final_usage)

    def openai_chat_completions_create(
        self,
        model: str,
        messages: list[dict],
        retry_times: int = 3,
        stream: bool = False,
        **kwargs: Any,
    ) -> str | None:
        if stream:
            kwargs.setdefault("stream_options", {})
            kwargs["stream_options"]["include_usage"] = True
            response = self.openai_client.chat.completions.create(
                model=model,
                messages=messages,
                **kwargs,
                stream=True,
            )
            return self._wrap_stream_with_usage_logging(response)
        while retry_times > 0:
            try:
                if "claude" in model:
                    kwargs["max_tokens"] = 64000
                    del kwargs["temperature"]

                if "gpt" in model.lower() or "o1" in model.lower():
                    if "max_tokens" in kwargs:
                        kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")

                if "kimi-k" in model.lower():
                    kwargs["extra_body"] = {"enable_thinking": True}

                response = self.openai_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **kwargs,
                )

                self._log_openai_usage(response)
                message = response.choices[0].message
                # content can come back as None (not just ""): qwen and other
                # thinking models return a null content when the answer budget
                # is consumed by reasoning. Treat that as empty rather than
                # crashing on None.strip().
                final_content = (message.content or "").strip()
                reasoning = getattr(message, "reasoning_content", None)
                # for k2.5, we keep its reasoning_content
                if "kimi-k" in model.lower() and reasoning:
                    final_content = f"<think>{reasoning.strip()}</think>\n{final_content}"
                # qwen is a thinking model: when finish_reason=length the visible
                # content is null but the actual answer (incl. any fenced JSON)
                # is in reasoning_content. Fall back to it so callers get
                # something parseable instead of None.
                elif "qwen" in model.lower() and not final_content and reasoning:
                    final_content = reasoning.strip()
                return final_content
            except Exception as e:
                error_msg = str(e)
                logger.warning(f"Error calling OpenAI API: {e}")

                # If the error is about max_tokens, retry with max_completion_tokens.
                if "max_tokens" in error_msg and "max_completion_tokens" in error_msg:
                    if "max_tokens" in kwargs:
                        logger.info("Retrying with max_completion_tokens instead of max_tokens")
                        kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                        continue  # retry without decrementing retry_times

                retry_times -= 1
                time.sleep(1)
        return None

    def _log_openai_usage(self, response: Any) -> None:
        """Log and track OpenAI API token usage."""
        if response.usage is None:
            return

        completion_tokens = response.usage.completion_tokens or 0
        prompt_tokens = response.usage.prompt_tokens or 0
        cached_tokens = 0

        if (
            hasattr(response.usage, "prompt_tokens_details")
            and response.usage.prompt_tokens_details
        ):
            cached_tokens = response.usage.prompt_tokens_details.cached_tokens or 0

        self._total_completion_tokens += completion_tokens
        self._total_prompt_tokens += prompt_tokens
        self._total_cached_tokens += cached_tokens

        logger.debug(
            f"OpenAI API usage: completion={completion_tokens}, prompt={prompt_tokens}, "
            f"cached={cached_tokens} | Total: completion={self._total_completion_tokens}, "
            f"prompt={self._total_prompt_tokens}, cached={self._total_cached_tokens}"
        )

    def get_total_token_usage(self) -> dict[str, int]:
        """Get the total token usage across all API calls."""
        return {
            "completion_tokens": self._total_completion_tokens,
            "prompt_tokens": self._total_prompt_tokens,
            "cached_tokens": self._total_cached_tokens,
            "total_tokens": self._total_completion_tokens + self._total_prompt_tokens,
        }

    def reset_token_usage(self) -> None:
        """Reset the token usage counters."""
        self._total_completion_tokens = 0
        self._total_prompt_tokens = 0
        self._total_cached_tokens = 0


class MCPAgent(BaseAgent):
    """Base for agents that may carry an MCP tool list. RelayAgent subclasses
    this (it passes `tools=[]`); the tool plumbing is inert on the GUI path but
    kept for signature compatibility with the ported agent code."""

    def __init__(self, tools: list[dict], *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.tools = tools

    def initialize(self, instruction: str) -> bool:
        """Initialize the agent with the given instruction."""
        self.instruction = instruction
        self.initialize_hook(self.instruction)
        logger.debug(f"initialized the agent with the given instruction: {self.instruction}")
        return True

    def reset_tools(self, tools: list[dict]) -> None:
        """Reset the tools for the agent."""
        self.tools = tools

    @abstractmethod
    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        """Generate the next action based on current observation."""
        raise NotImplementedError("predict method is not implemented")
