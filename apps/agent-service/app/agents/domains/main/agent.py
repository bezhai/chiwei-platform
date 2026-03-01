"""主聊天 Agent"""

import asyncio
import copy
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime

from langchain.messages import AIMessageChunk
from langfuse import get_client as get_langfuse
from langfuse import propagate_attributes

from app.agents.core import (
    AgentContext,
    ChatAgent,
    FeatureFlags,
    MediaContext,
    MessageContext,
)
from app.agents.domains.main.context_builder import build_chat_context
from app.agents.domains.main.tools import ALL_TOOLS
from app.agents.graphs.pre import Complexity, run_pre
from app.orm.crud import get_gray_config, get_message_content
from app.services.memory_context import build_memory_context
from app.types.chat import ChatStreamChunk
from app.utils.async_interval import AsyncIntervalChecker
from app.utils.content_parser import parse_content
from app.utils.status_processor import AIMessageChunkProcessor

logger = logging.getLogger(__name__)

YIELD_INTERVAL = 0.5

# 统一的拒绝响应
GUARD_REJECT_MESSAGE = "你发了一些赤尾不想讨论的话题呢~"

# 复杂度行为引导
COMPLEXITY_HINTS = {
    Complexity.SIMPLE: "【简洁模式】倾向于直接回答或单次工具调用，快速响应用户。",
    Complexity.COMPLEX: "【深度模式】可以多步推理，充分利用工具收集信息后再综合回答。",
    Complexity.SUPER_COMPLEX: "【研究模式】这是一个复杂的研究任务，可以进行深入分析和多轮工具调用。",
}


async def stream_chat(
    message_id: str, session_id: str | None = None
) -> AsyncGenerator[ChatStreamChunk, None]:
    """主聊天流式响应入口

    Args:
        message_id: 触发消息的 ID
        session_id: 会话追踪 ID（由 main-server 生成）

    Yields:
        ChatStreamChunk: 聊天流式响应块
    """
    # 0. 创建父 trace，pre 和 main 的 CallbackHandler 会自动嵌套其下
    langfuse = get_langfuse()
    request_id = session_id or str(uuid.uuid4())

    with langfuse.start_as_current_observation(as_type="span", name="chat-request"):
        with propagate_attributes(session_id=request_id):
            # 1. 获取消息内容
            raw_content = await get_message_content(message_id)
            if not raw_content:
                logger.warning(f"No message found for message_id: {message_id}")
                yield ChatStreamChunk(content="抱歉，未找到相关消息记录")
                return

            # 解析 v2 内容，提取纯文本供 pre 使用
            parsed = parse_content(raw_content)

            # 2. 获取 gray_config（需要提前获取以决定 pre 模式）
            gray_config = (await get_gray_config(message_id)) or {}
            pre_blocking = gray_config.get("pre_blocking", "false")

            # 3. 启动 pre task（create_task 复制当前 context，继承父 trace）
            pre_task = asyncio.create_task(run_pre(parsed.render()))

            if pre_blocking != "false":
                # === 保守模式：等 pre 完成再继续 ===
                pre_result = await pre_task

                if pre_result["is_blocked"]:
                    logger.info(
                        f"消息被拦截: message_id={message_id}, "
                        f"reason={pre_result['block_reason']}"
                    )
                    yield ChatStreamChunk(content=GUARD_REJECT_MESSAGE)
                    return

                complexity_result = pre_result["complexity_result"]
                complexity = (
                    complexity_result.complexity
                    if complexity_result
                    else Complexity.SIMPLE
                )
                logger.info(f"复杂度路由: complexity={complexity.value}")

                async for chunk in _build_and_stream(
                    message_id, complexity, gray_config, request_id
                ):
                    yield chunk
            else:
                # === 并行模式：pre 在后台运行，主模型同时流式生成 ===
                logger.info(f"并行模式启动: message_id={message_id}")
                raw_stream = _build_and_stream(
                    message_id, Complexity.SIMPLE, gray_config, request_id
                )

                async for chunk in _buffer_until_pre(raw_stream, pre_task, message_id):
                    yield chunk


async def _buffer_until_pre(
    raw_stream: AsyncGenerator[ChatStreamChunk, None],
    pre_task: asyncio.Task,
    message_id: str,
) -> AsyncGenerator[ChatStreamChunk, None]:
    """用 pre_task 结果守护一个原始 chunk 流：缓冲直到 pre 通过后释放，被拦截则丢弃。

    - 每收到一个 chunk 时检查 pre_task 是否已完成
    - pre 通过 → 释放全部 buffer，后续直接 yield
    - pre 拦截 → 丢弃 buffer，yield 拒绝消息
    - 流结束后 pre 仍未完成 → await 再决定
    """
    buffer: list[ChatStreamChunk] = []
    pre_resolved = False

    try:
        async for chunk in raw_stream:
            # 每个 chunk 到达时，探测 pre 是否已完成
            if not pre_resolved and pre_task.done():
                pre_result = pre_task.result()
                pre_resolved = True

                if pre_result["is_blocked"]:
                    logger.info(
                        f"并行模式拦截: message_id={message_id}, "
                        f"reason={pre_result['block_reason']}"
                    )
                    yield ChatStreamChunk(content=GUARD_REJECT_MESSAGE)
                    return

                # pre 通过，释放 buffer
                for buffered in buffer:
                    yield buffered
                buffer.clear()

            if pre_resolved:
                yield chunk
            else:
                # copy: accumulate_chunk 是同一个可变对象被反复 yield，
                # 需要快照当前状态（content 是 str，浅拷贝即可）
                buffer.append(copy.copy(chunk))
    except Exception:
        # 确保 pre_task 不会悬空
        if not pre_task.done():
            pre_task.cancel()
        raise

    # 流结束后 pre 仍未完成 → await 再处理
    if not pre_resolved:
        try:
            pre_result = await pre_task
        except Exception as e:
            logger.error(f"pre_task 异常: {e}")
            # pre 异常时放行，释放 buffer
            for buffered in buffer:
                yield buffered
            return

        if pre_result["is_blocked"]:
            logger.info(
                f"并行模式拦截（流结束后）: message_id={message_id}, "
                f"reason={pre_result['block_reason']}"
            )
            yield ChatStreamChunk(content=GUARD_REJECT_MESSAGE)
            return

        for buffered in buffer:
            yield buffered


async def _build_and_stream(
    message_id: str,
    complexity: Complexity,
    gray_config: dict,
    session_id: str | None = None,
) -> AsyncGenerator[ChatStreamChunk, None]:
    """构建 agent + 上下文，执行流式生成（两种模式共用）"""
    # 构建 prompt 变量（注入复杂度引导）
    now = datetime.now()
    prompt_vars = {
        "complexity_hint": COMPLEXITY_HINTS.get(complexity, ""),
        "curr_date": now.strftime("%Y-%m-%d"),
        "curr_time": now.strftime("%H:%M"),
        "user_info": "",
    }

    # 创建 agent
    model_id = "main-chat-model"
    if gray_config.get("main_model"):
        model_id = str(gray_config.get("main_model"))

    agent = ChatAgent(
        "main",
        ALL_TOOLS,
        model_id=model_id,
        trace_name="main",
    )

    # 构建上下文
    (
        messages,
        image_urls,
        chat_id,
        trigger_username,
        chat_type,
        trigger_user_id,
    ) = await build_chat_context(message_id)

    if not messages:
        logger.warning(f"No results found for message_id: {message_id}")
        yield ChatStreamChunk(content="抱歉，未找到相关消息记录")
        return

    # 私聊时注入用户信息，帮助模型了解对话对象
    if chat_type == "p2p" and trigger_username:
        prompt_vars["user_info"] = f"你正在和 {trigger_username} 私聊。"

    # 注入记忆上下文
    if trigger_user_id:
        try:
            memory_text = await build_memory_context(
                trigger_user_id, chat_id, chat_type
            )
            prompt_vars["memory_context"] = memory_text
        except Exception as e:
            logger.error(f"Failed to build memory context: {e}")
            prompt_vars["memory_context"] = ""
    else:
        prompt_vars["memory_context"] = ""

    accumulate_chunk = ChatStreamChunk(
        content="",
        reason_content="",
    )

    interval_checker = AsyncIntervalChecker(YIELD_INTERVAL)
    processor = AIMessageChunkProcessor()
    should_continue = True

    try:
        async for token in agent.stream(
            messages,
            context=AgentContext(
                message=MessageContext(message_id=message_id, chat_id=chat_id),
                media=MediaContext(image_urls=image_urls or []),
                features=FeatureFlags(flags=gray_config or {}),
            ),
            prompt_vars=prompt_vars,
        ):
            if isinstance(token, AIMessageChunk):
                finish_reason = token.response_metadata.get("finish_reason")

                if finish_reason == "stop":
                    yield accumulate_chunk
                elif finish_reason == "content_filter":
                    yield ChatStreamChunk(content="小尾有点不想讨论这个话题呢~")
                    should_continue = False
                elif finish_reason == "length":
                    yield ChatStreamChunk(content="(后续内容被截断)")
                    should_continue = False

                if not should_continue:
                    continue

                status_message = processor.process_chunk(token)
                if status_message:
                    yield ChatStreamChunk(status_message=status_message)

                accumulate_chunk.content += token.text or ""

                if interval_checker.check():
                    yield accumulate_chunk

        yield accumulate_chunk

        # Fire-and-forget: publish to post safety check queue
        full_response = accumulate_chunk.content or ""
        if full_response and session_id:
            asyncio.create_task(
                _publish_post_check(session_id, full_response, chat_id, message_id)
            )

    except Exception as e:
        import traceback

        logger.error(f"stream_chat error: {str(e)}\n{traceback.format_exc()}")
        yield ChatStreamChunk(content="赤尾好像遇到了一些问题呢QAQ")


async def _publish_post_check(
    session_id: str,
    response_text: str,
    chat_id: str,
    trigger_message_id: str,
) -> None:
    """发布 post safety check 消息到 RabbitMQ"""
    try:
        from app.clients.rabbitmq import RK_SAFETY_CHECK, RabbitMQClient
        from app.utils.middlewares.trace import get_lane

        client = RabbitMQClient.get_instance()
        await client.publish(
            RK_SAFETY_CHECK,
            {
                "session_id": session_id,
                "response_text": response_text,
                "chat_id": chat_id,
                "trigger_message_id": trigger_message_id,
                "lane": get_lane(),
            },
        )
        logger.info(f"Published post safety check: session_id={session_id}")
    except Exception as e:
        logger.error(f"Failed to publish post safety check: {e}")
