"""ChatAgent - 核心聊天代理实现

保留原有的 ChatAgent 实现，作为核心的 Agent 抽象。
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from datetime import datetime

from langchain.agents import create_agent
from langchain.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.runnables import RunnableConfig
from langfuse.langchain import CallbackHandler
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)

from app.agents.core.context import AgentContext
from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder

logger = logging.getLogger(__name__)

# Agent 级可重试异常（SDK 级重试由 max_retries 处理，这里处理更上层的重试）
RETRYABLE_EXCEPTIONS = (
    APITimeoutError,
    APIConnectionError,
    InternalServerError,
    RateLimitError,
)

# 默认 agent 级重试配置
_DEFAULT_MAX_RETRIES = 2
_BACKOFF_BASE = 2  # 秒
_BACKOFF_MAX = 8  # 秒


class ChatAgent:
    """核心聊天代理

    封装了 LangChain Agent 的创建和调用逻辑，提供统一的流式和同步接口。
    """

    def __init__(
        self,
        prompt_id: str,
        tools: list,
        model_id: str | None = None,
        trace_name: str | None = None,
    ):
        self.model_id = model_id
        self.prompt_id = prompt_id
        self.tools = tools
        self.trace_name = trace_name
        self._agent = None  # 缓存agent实例

    async def _init_agent(self, **prompt_vars):
        """初始化 Agent（延迟初始化）"""
        langfuse_prompt = get_prompt(self.prompt_id)

        assert self.model_id is not None, "Model ID must be specified"

        model = await ModelBuilder.build_chat_model(self.model_id)

        prompt = langfuse_prompt.get_langchain_prompt(
            currDate=datetime.now().strftime("%Y-%m-%d"),
            currTime=datetime.now().strftime("%H:%M:%S"),
            **prompt_vars,
        )

        self._agent = create_agent(
            model, self.tools, system_prompt=prompt, context_schema=AgentContext
        )

    def _build_config(self, parent_config: RunnableConfig | None = None) -> dict:
        """构建运行配置，支持继承父级 config 的 callbacks"""
        if parent_config:
            # 从父级 config 继承 callbacks，保持 trace 链路
            config: dict = dict(parent_config)
            if self.trace_name:
                config["run_name"] = self.trace_name
        else:
            # 根 agent：创建新的 langfuse handler
            config = {"callbacks": [CallbackHandler(update_trace=True)]}
            if self.trace_name:
                config["run_name"] = self.trace_name
        return config

    async def stream(
        self,
        messages: list,
        context: AgentContext | None = None,
        prompt_vars: dict | None = None,
        config: RunnableConfig | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> AsyncGenerator[AIMessageChunk | ToolMessage, None]:
        """流式生成响应

        Args:
            messages: 输入消息列表
            context: Agent 执行上下文
            prompt_vars: Prompt 模板变量
            config: 运行配置（用于继承 trace）
            max_retries: agent 级重试次数（默认 2），仅对可重试异常生效

        Yields:
            AIMessageChunk 或 ToolMessage
        """
        await self._init_agent(**(prompt_vars or {}))

        run_config = self._build_config(config)

        assert self._agent is not None  # for mypy type checking

        for attempt in range(1, max_retries + 1):
            tokens_yielded = False
            try:
                async for (
                    token,
                    _,
                ) in self._agent.astream(  # pyright: ignore[reportOptionalMemberAccess]
                    {"messages": messages},
                    context=context,
                    stream_mode="messages",
                    config=run_config,  # pyright: ignore[reportArgumentType]
                ):
                    tokens_yielded = True
                    yield token  # type: ignore
                return  # 成功完成
            except RETRYABLE_EXCEPTIONS as e:
                if tokens_yielded:
                    raise  # 已输出 token 则不重试，防止内容重复
                if attempt < max_retries:
                    delay = min(_BACKOFF_BASE**attempt, _BACKOFF_MAX)
                    logger.warning(
                        f"stream() attempt {attempt}/{max_retries} failed: {e}, "
                        f"retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
                else:
                    raise

    async def run(
        self,
        messages: list,
        context: AgentContext | None = None,
        prompt_vars: dict | None = None,
        config: RunnableConfig | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> AIMessage:
        """同步执行并返回最终响应

        Args:
            messages: 输入消息列表
            context: Agent 执行上下文
            prompt_vars: Prompt 模板变量
            config: 运行配置（用于继承 trace）
            max_retries: agent 级重试次数（默认 2），仅对可重试异常生效

        Returns:
            最终的 AI 响应消息
        """
        await self._init_agent(**(prompt_vars or {}))

        run_config = self._build_config(config)

        assert self._agent is not None  # for mypy type checking

        for attempt in range(1, max_retries + 1):
            try:
                all_message = await self._agent.ainvoke(  # pyright: ignore[reportOptionalMemberAccess]
                    {"messages": messages},
                    context=context,
                    config=run_config,  # pyright: ignore[reportArgumentType]
                )
                return all_message["messages"][-1]
            except RETRYABLE_EXCEPTIONS as e:
                if attempt < max_retries:
                    delay = min(_BACKOFF_BASE**attempt, _BACKOFF_MAX)
                    logger.warning(
                        f"run() attempt {attempt}/{max_retries} failed: {e}, "
                        f"retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
                else:
                    raise

        # Unreachable but satisfies type checker
        raise RuntimeError("Unexpected: all retry attempts exhausted without raise")
