"""LLMService — 统一的 LLM 调用入口

所有 LLM 调用都应通过此 Service，确保：
1. 统一 Langfuse trace 接入（禁止裸跑）
2. 统一重试逻辑（瞬时错误指数退避）
3. 统一 prompt 管理（Langfuse prompt → SystemMessage）
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, SystemMessage
from langfuse.langchain import CallbackHandler
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder

logger = logging.getLogger(__name__)

# 可重试异常（与 ChatAgent 保持一致）
RETRYABLE_EXCEPTIONS = (
    APITimeoutError,
    APIConnectionError,
    InternalServerError,
    RateLimitError,
)

_DEFAULT_MAX_RETRIES = 2
_BACKOFF_BASE = 2  # 秒
_BACKOFF_MAX = 8  # 秒


def _build_messages(
    system_prompt: str, messages: list[dict | BaseMessage]
) -> list[BaseMessage | dict]:
    """将 system_prompt 作为 SystemMessage 拼到 messages 前面"""
    return [SystemMessage(content=system_prompt)] + list(messages)


def _build_config(
    trace_name: str | None = None,
    parent_run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建 LangChain config，包含 Langfuse CallbackHandler"""
    cb_kwargs: dict[str, Any] = {}
    if parent_run_id:
        cb_kwargs["trace_id"] = parent_run_id
    if metadata:
        cb_kwargs["metadata"] = metadata

    config: dict[str, Any] = {
        "callbacks": [CallbackHandler(**cb_kwargs)],
    }
    if trace_name:
        config["run_name"] = trace_name
    return config


async def _retry_invoke(
    invoke_fn: Any,
    full_messages: list,
    config: dict,
    max_retries: int,
    trace_name: str,
    method_name: str,
) -> Any:
    """重试包装器，用于 run() 和 extract()"""
    for attempt in range(1, max_retries + 1):
        try:
            return await invoke_fn(full_messages, config=config)
        except RETRYABLE_EXCEPTIONS as e:
            if attempt < max_retries:
                delay = min(_BACKOFF_BASE**attempt, _BACKOFF_MAX)
                logger.warning(
                    "LLMService.%s() attempt %d/%d failed: %s, "
                    "retrying in %ds",
                    method_name,
                    attempt,
                    max_retries,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                raise

    # 不可达，满足类型检查
    raise RuntimeError("Unexpected: all retry attempts exhausted without raise")


class LLMService:
    """统一的 LLM 调用入口

    提供三个静态方法：
    - run: 同步调用，返回 AIMessage
    - stream: 流式调用，返回 AsyncGenerator[AIMessageChunk]
    - extract: 结构化提取，返回 Pydantic BaseModel
    """

    @staticmethod
    async def run(
        prompt_id: str,
        prompt_vars: dict[str, Any],
        messages: list[dict | BaseMessage],
        *,
        model_id: str,
        trace_name: str | None = None,
        parent_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        model_kwargs: dict[str, Any] | None = None,
    ) -> AIMessage:
        """同步调用 LLM，返回 AIMessage

        Args:
            prompt_id: Langfuse prompt ID
            prompt_vars: prompt 模板变量
            messages: 用户消息列表
            model_id: 模型 ID（对应 ModelBuilder）
            trace_name: Langfuse trace 名称
            parent_run_id: 父 trace ID（用于嵌套 trace）
            metadata: Langfuse metadata
            max_retries: 最大重试次数（仅对瞬时错误生效）
            model_kwargs: 传递给 ModelBuilder 的额外参数（如 reasoning_effort）

        Returns:
            AIMessage
        """
        model = await ModelBuilder.build_chat_model(
            model_id, **(model_kwargs or {})
        )
        prompt = get_prompt(prompt_id)
        system_prompt = prompt.compile(**prompt_vars)
        full_messages = _build_messages(system_prompt, messages)
        config = _build_config(trace_name, parent_run_id, metadata)

        return await _retry_invoke(
            model.ainvoke,
            full_messages,
            config,
            max_retries,
            trace_name or "run",
            "run",
        )

    @staticmethod
    async def stream(
        prompt_id: str,
        prompt_vars: dict[str, Any],
        messages: list[dict | BaseMessage],
        *,
        model_id: str,
        trace_name: str | None = None,
        parent_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        model_kwargs: dict[str, Any] | None = None,
    ) -> AsyncGenerator[AIMessageChunk, None]:
        """流式调用 LLM，yield AIMessageChunk

        Args:
            prompt_id: Langfuse prompt ID
            prompt_vars: prompt 模板变量
            messages: 用户消息列表
            model_id: 模型 ID
            trace_name: Langfuse trace 名称
            parent_run_id: 父 trace ID
            metadata: Langfuse metadata
            model_kwargs: 传递给 ModelBuilder 的额外参数

        Yields:
            AIMessageChunk
        """
        model = await ModelBuilder.build_chat_model(
            model_id, **(model_kwargs or {})
        )
        prompt = get_prompt(prompt_id)
        system_prompt = prompt.compile(**prompt_vars)
        full_messages = _build_messages(system_prompt, messages)
        config = _build_config(trace_name, parent_run_id, metadata)

        async for chunk in model.astream(full_messages, config=config):
            yield chunk  # type: ignore[misc]

    @staticmethod
    async def extract(
        prompt_id: str,
        prompt_vars: dict[str, Any],
        messages: list[dict | BaseMessage],
        schema: type[BaseModel],
        *,
        model_id: str,
        trace_name: str | None = None,
        parent_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        model_kwargs: dict[str, Any] | None = None,
    ) -> BaseModel:
        """结构化提取，返回 Pydantic model 实例

        Args:
            prompt_id: Langfuse prompt ID
            prompt_vars: prompt 模板变量
            messages: 用户消息列表
            schema: Pydantic BaseModel 类（用于 structured output）
            model_id: 模型 ID
            trace_name: Langfuse trace 名称
            parent_run_id: 父 trace ID
            metadata: Langfuse metadata
            max_retries: 最大重试次数
            model_kwargs: 传递给 ModelBuilder 的额外参数（如 reasoning_effort）

        Returns:
            schema 指定的 Pydantic model 实例
        """
        model = await ModelBuilder.build_chat_model(
            model_id, **(model_kwargs or {})
        )
        structured_model = model.with_structured_output(schema)

        prompt = get_prompt(prompt_id)
        system_prompt = prompt.compile(**prompt_vars)
        full_messages = _build_messages(system_prompt, messages)
        config = _build_config(trace_name, parent_run_id, metadata)

        return await _retry_invoke(
            structured_model.ainvoke,
            full_messages,
            config,
            max_retries,
            trace_name or "extract",
            "extract",
        )
