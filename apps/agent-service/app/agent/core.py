"""Unified Agent — single entry point for all LLM interactions.

Every ``run()`` / ``stream()`` goes through LangGraph ``create_agent``.
Having tools or not is just a parameter — no separate code paths.

The only exception is ``extract()`` which needs ``model.with_structured_output()``
(a model-level feature that LangGraph agents don't expose).

Usage::

    from app.agent.core import Agent, AgentConfig

    CFG = AgentConfig("afterthought_conversation", "diary-model", "afterthought")

    result = await Agent(CFG).run(messages=[...], prompt_vars={...})

    async for chunk in Agent(CFG, tools=ALL_TOOLS).stream(messages=[...]):
        ...

    data = await Agent(CFG).extract(Model, messages=[...])
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    SystemMessage,
    ToolMessage,
)
from langfuse.langchain import CallbackHandler
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel

from app.agent.context import AgentContext
from app.agent.models import build_chat_model
from app.agent.prompts import get_prompt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

RETRYABLE_EXCEPTIONS = (
    APITimeoutError,
    APIConnectionError,
    InternalServerError,
    RateLimitError,
)

_DEFAULT_MAX_RETRIES = 2
_BACKOFF_BASE = 2  # seconds
_BACKOFF_MAX = 8  # seconds

# LangGraph recursion limit: 12 steps ~ 6 tool calls
_DEFAULT_RECURSION_LIMIT = 12


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Immutable configuration for an agent.

    Each domain module defines its own config constants.
    Use ``dataclasses.replace(cfg, model_id="...")`` for per-call overrides.
    """

    prompt_id: str
    model_id: str
    trace_name: str | None = None
    recursion_limit: int = _DEFAULT_RECURSION_LIMIT


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class Agent:
    """Unified thinking entry point.

    ``run`` / ``stream`` always go through LangGraph ``create_agent``.
    Having tools or not is just a parameter difference, not a code path difference.

    ``extract`` is the sole exception — it needs ``model.with_structured_output()``,
    which is a model-level API that LangGraph doesn't expose.
    """

    def __init__(
        self,
        config: AgentConfig,
        *,
        tools: list[Any] | None = None,
        model_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._cfg = config
        self._tools = tools or []
        self._model_kwargs = model_kwargs or {}

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    async def _build_agent(self, prompt_vars: dict[str, Any]) -> Any:
        """Create a LangGraph agent with the configured prompt and tools."""
        if not self._cfg.prompt_id:
            raise ValueError(
                f"Agent({self._cfg.trace_name}).run/stream requires a non-empty "
                f"prompt_id. Guard agents (empty prompt_id) should use extract()."
            )
        langfuse_prompt = get_prompt(self._cfg.prompt_id)
        model = await build_chat_model(self._cfg.model_id, **self._model_kwargs)
        prompt = langfuse_prompt.get_langchain_prompt(
            currDate=datetime.now().strftime("%Y-%m-%d"),
            currTime=datetime.now().strftime("%H:%M:%S"),
            **prompt_vars,
        )
        return create_agent(
            model,
            self._tools,
            system_prompt=prompt,
            context_schema=AgentContext,
        )

    def _build_config(self) -> dict[str, Any]:
        """Build LangChain config with Langfuse tracing."""
        config: dict[str, Any] = {
            "callbacks": [CallbackHandler(update_trace=True)],
            "recursion_limit": self._cfg.recursion_limit,
        }
        if self._cfg.trace_name:
            config["run_name"] = self._cfg.trace_name
        return config

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def run(
        self,
        messages: list[dict[str, Any] | BaseMessage],
        *,
        prompt_vars: dict[str, Any] | None = None,
        context: AgentContext | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> AIMessage:
        """Execute and return the final ``AIMessage``."""
        agent = await self._build_agent(prompt_vars or {})
        config = self._build_config()

        async def _invoke(msgs: Any, *, config: Any) -> AIMessage:
            result = await agent.ainvoke(
                {"messages": msgs}, context=context, config=config
            )
            return result["messages"][-1]

        return await _retry(
            _invoke,
            messages,
            config,
            max_retries=max_retries,
            label=f"Agent({self._cfg.trace_name}).run",
        )

    async def stream(
        self,
        messages: list[dict[str, Any] | BaseMessage],
        *,
        prompt_vars: dict[str, Any] | None = None,
        context: AgentContext | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> AsyncGenerator[AIMessageChunk | ToolMessage, None]:
        """Stream tokens.

        Retry caveat: once tokens have been yielded, retrying would cause
        duplicate content — so the error is re-raised instead.
        """
        agent = await self._build_agent(prompt_vars or {})
        config = self._build_config()

        for attempt in range(1, max_retries + 1):
            tokens_yielded = False
            try:
                async for token, _ in agent.astream(
                    {"messages": messages},
                    context=context,
                    stream_mode="messages",
                    config=config,
                ):
                    tokens_yielded = True
                    yield token
                return
            except RETRYABLE_EXCEPTIONS as e:
                if tokens_yielded:
                    raise
                if attempt < max_retries:
                    delay = min(_BACKOFF_BASE**attempt, _BACKOFF_MAX)
                    logger.warning(
                        "Agent(%s).stream() attempt %d/%d failed: %s, retrying in %ds",
                        self._cfg.trace_name,
                        attempt,
                        max_retries,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise

    async def extract(
        self,
        response_model: type[BaseModel],
        messages: list[dict[str, Any] | BaseMessage],
        *,
        prompt_vars: dict[str, Any] | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> BaseModel:
        """Structured output — return a Pydantic model instance.

        The sole path that bypasses LangGraph: needs
        ``model.with_structured_output()`` which is a model-level API.
        """
        model = await build_chat_model(self._cfg.model_id, **self._model_kwargs)
        structured = model.with_structured_output(response_model)

        prompt_id = self._cfg.prompt_id
        if prompt_id:
            system = get_prompt(prompt_id).compile(**(prompt_vars or {}))
            messages = [SystemMessage(content=system), *messages]

        config = self._build_config()
        return await _retry(
            structured.ainvoke,
            messages,
            config,
            max_retries=max_retries,
            label=f"Agent({self._cfg.trace_name}).extract",
        )


# ---------------------------------------------------------------------------
# Shared retry helper
# ---------------------------------------------------------------------------


async def _retry(
    invoke_fn: Any,
    full_messages: list[Any],
    config: dict[str, Any],
    *,
    max_retries: int,
    label: str,
) -> Any:
    """Exponential-backoff retry wrapper for non-streaming invoke calls."""
    for attempt in range(1, max_retries + 1):
        try:
            return await invoke_fn(full_messages, config=config)
        except RETRYABLE_EXCEPTIONS as e:
            if attempt < max_retries:
                delay = min(_BACKOFF_BASE**attempt, _BACKOFF_MAX)
                logger.warning(
                    "%s attempt %d/%d failed: %s, retrying in %ds",
                    label,
                    attempt,
                    max_retries,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                raise

    raise RuntimeError("Unreachable: all retry attempts exhausted")
