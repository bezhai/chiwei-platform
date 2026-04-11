"""Unified Agent — single entry point for all LLM interactions.

``Agent`` merges the old ChatAgent (with tools / LangGraph) and LLMService
(without tools) into **one** class.  Whether the call is agentic or plain
is decided by a single parameter: ``tools``.

Usage examples::

    from app.agent.core import Agent, AgentConfig

    CFG = AgentConfig("afterthought_conversation", "diary-model", "afterthought")

    # Non-agentic
    result = await Agent(CFG).run(prompt_vars={...}, messages=[...])

    # Agentic with tools
    async for chunk in Agent(MAIN_CFG, tools=ALL_TOOLS).stream(...):
        ...

    # Structured output
    data = await Agent(CFG).extract(FilterResult, messages=[...])

    # Override model_id for a single call
    from dataclasses import replace
    result = await Agent(replace(CFG, model_id="gpt-4o")).run(...)
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
from langchain_core.runnables import RunnableConfig
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prepend_system(
    system_prompt: str | None,
    messages: list[dict[str, Any] | BaseMessage],
) -> list[dict[str, Any] | BaseMessage]:
    """Prepend a SystemMessage when *system_prompt* is not None."""
    if system_prompt is None:
        return list(messages)
    return [SystemMessage(content=system_prompt), *messages]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class Agent:
    """Unified thinking entry point.

    Parameters
    ----------
    config:
        An ``AgentConfig`` specifying prompt_id, model_id, and trace_name.
        Each domain module defines its own config constants.
    tools:
        If provided, the agent uses LangGraph ``create_agent`` for multi-step
        reasoning (the agentic path).  If ``None``, uses ``model.ainvoke``
        directly (the plain LLM path).
    model_kwargs:
        Extra keyword arguments forwarded to ``build_chat_model``
        (e.g. ``reasoning_effort``, ``temperature``).
    """

    def __init__(
        self,
        config: AgentConfig,
        *,
        tools: list[Any] | None = None,
        model_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._cfg = config
        self._tools = tools
        self._model_kwargs = model_kwargs or {}

    # ------------------------------------------------------------------
    # config builders
    # ------------------------------------------------------------------

    def _build_agentic_config(
        self, parent_config: RunnableConfig | None = None
    ) -> dict[str, Any]:
        """Build LangChain config for the agentic (LangGraph) path."""
        if parent_config:
            config: dict[str, Any] = dict(parent_config)
            if self._cfg.trace_name:
                config["run_name"] = self._cfg.trace_name
        else:
            config = {"callbacks": [CallbackHandler(update_trace=True)]}
            if self._cfg.trace_name:
                config["run_name"] = self._cfg.trace_name
        config.setdefault("recursion_limit", _DEFAULT_RECURSION_LIMIT)
        return config

    def _build_plain_config(
        self,
        parent_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build LangChain config for the plain (non-agentic) path."""
        cb_kwargs: dict[str, Any] = {}
        if parent_run_id:
            cb_kwargs["trace_id"] = parent_run_id
        if metadata:
            cb_kwargs["metadata"] = metadata

        config: dict[str, Any] = {"callbacks": [CallbackHandler(**cb_kwargs)]}
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
        config: RunnableConfig | None = None,
        parent_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> AIMessage:
        """Execute and return the final ``AIMessage``.

        - Agentic path (tools present): delegates to LangGraph agent's
          ``ainvoke``, returns the last message.
        - Plain path (no tools): calls ``model.ainvoke`` directly.
        """
        if self._tools is not None:
            return await self._run_agentic(
                messages,
                prompt_vars=prompt_vars or {},
                context=context,
                config=config,
                max_retries=max_retries,
            )

        return await self._run_plain(
            messages,
            prompt_vars=prompt_vars or {},
            parent_run_id=parent_run_id,
            metadata=metadata,
            max_retries=max_retries,
        )

    async def stream(
        self,
        messages: list[dict[str, Any] | BaseMessage],
        *,
        prompt_vars: dict[str, Any] | None = None,
        context: AgentContext | None = None,
        config: RunnableConfig | None = None,
        parent_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> AsyncGenerator[AIMessageChunk | ToolMessage, None]:
        """Stream tokens.

        - Agentic path: yields ``AIMessageChunk`` and ``ToolMessage`` via
          LangGraph ``astream``.
        - Plain path: yields ``AIMessageChunk`` via ``model.astream``.

        Retry caveat: once tokens have been yielded, retrying would cause
        duplicate content — so the error is re-raised instead.
        """
        if self._tools is not None:
            async for chunk in self._stream_agentic(
                messages,
                prompt_vars=prompt_vars or {},
                context=context,
                config=config,
                max_retries=max_retries,
            ):
                yield chunk
            return

        async for chunk in self._stream_plain(
            messages,
            prompt_vars=prompt_vars or {},
            parent_run_id=parent_run_id,
            metadata=metadata,
        ):
            yield chunk

    async def extract(
        self,
        response_model: type[BaseModel],
        messages: list[dict[str, Any] | BaseMessage],
        *,
        prompt_vars: dict[str, Any] | None = None,
        parent_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> BaseModel:
        """Structured output — return a Pydantic model instance.

        Always uses the plain (non-agentic) path, calling
        ``model.with_structured_output(response_model).ainvoke(...)``.
        """
        model = await build_chat_model(self._cfg.model_id, **self._model_kwargs)
        structured_model = model.with_structured_output(response_model)

        system_prompt = self._compile_prompt(prompt_vars or {})
        full_messages = _prepend_system(system_prompt, messages)
        run_config = self._build_plain_config(parent_run_id, metadata)

        return await _retry(
            structured_model.ainvoke,
            full_messages,
            run_config,
            max_retries=max_retries,
            label=f"Agent({self._cfg.trace_name}).extract",
        )

    # ------------------------------------------------------------------
    # agentic path (with tools / LangGraph)
    # ------------------------------------------------------------------

    async def _build_langgraph_agent(self, prompt_vars: dict[str, Any]) -> Any:
        """Create a LangGraph agent with the configured tools and prompt."""
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

    async def _run_agentic(
        self,
        messages: list[dict[str, Any] | BaseMessage],
        *,
        prompt_vars: dict[str, Any],
        context: AgentContext | None,
        config: RunnableConfig | None,
        max_retries: int,
    ) -> AIMessage:
        agent = await self._build_langgraph_agent(prompt_vars)
        run_config = self._build_agentic_config(config)

        async def _invoke(msgs: Any, *, config: Any) -> AIMessage:
            result = await agent.ainvoke(
                {"messages": msgs}, context=context, config=config
            )
            return result["messages"][-1]

        return await _retry(
            _invoke,
            messages,
            run_config,
            max_retries=max_retries,
            label=f"Agent({self._cfg.trace_name}).run",
        )

    async def _stream_agentic(
        self,
        messages: list[dict[str, Any] | BaseMessage],
        *,
        prompt_vars: dict[str, Any],
        context: AgentContext | None,
        config: RunnableConfig | None,
        max_retries: int,
    ) -> AsyncGenerator[AIMessageChunk | ToolMessage, None]:
        agent = await self._build_langgraph_agent(prompt_vars)
        run_config = self._build_agentic_config(config)

        for attempt in range(1, max_retries + 1):
            tokens_yielded = False
            try:
                async for token, _ in agent.astream(
                    {"messages": messages},
                    context=context,
                    stream_mode="messages",
                    config=run_config,
                ):
                    tokens_yielded = True
                    yield token
                return  # success
            except RETRYABLE_EXCEPTIONS as e:
                if tokens_yielded:
                    raise  # already yielded tokens -> no safe retry
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

    # ------------------------------------------------------------------
    # plain path (no tools)
    # ------------------------------------------------------------------

    def _compile_prompt(self, prompt_vars: dict[str, Any]) -> str | None:
        """Compile the Langfuse prompt, or return None if prompt_id is None."""
        prompt_id = self._cfg.prompt_id
        if not prompt_id:
            return None
        langfuse_prompt = get_prompt(prompt_id)
        return langfuse_prompt.compile(**prompt_vars)

    async def _run_plain(
        self,
        messages: list[dict[str, Any] | BaseMessage],
        *,
        prompt_vars: dict[str, Any],
        parent_run_id: str | None,
        metadata: dict[str, Any] | None,
        max_retries: int,
    ) -> AIMessage:
        model = await build_chat_model(self._cfg.model_id, **self._model_kwargs)
        system_prompt = self._compile_prompt(prompt_vars)
        full_messages = _prepend_system(system_prompt, messages)
        run_config = self._build_plain_config(parent_run_id, metadata)

        return await _retry(
            model.ainvoke,
            full_messages,
            run_config,
            max_retries=max_retries,
            label=f"Agent({self._cfg.trace_name}).run",
        )

    async def _stream_plain(
        self,
        messages: list[dict[str, Any] | BaseMessage],
        *,
        prompt_vars: dict[str, Any],
        parent_run_id: str | None,
        metadata: dict[str, Any] | None,
    ) -> AsyncGenerator[AIMessageChunk, None]:
        model = await build_chat_model(self._cfg.model_id, **self._model_kwargs)
        system_prompt = self._compile_prompt(prompt_vars)
        full_messages = _prepend_system(system_prompt, messages)
        run_config = self._build_plain_config(parent_run_id, metadata)

        async for chunk in model.astream(full_messages, config=run_config):
            yield chunk  # type: ignore[misc]


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
