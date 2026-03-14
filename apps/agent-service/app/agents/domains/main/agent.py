"""主聊天 Agent"""

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncGenerator

from langchain.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
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
from app.services.memory_context import build_diary_context, build_impression_context
from app.utils.content_parser import parse_content

logger = logging.getLogger(__name__)

# 统一的拒绝响应
GUARD_REJECT_MESSAGE = "你发了一些赤尾不想讨论的话题呢~"

# 分段标记（consumer 侧检测并拆分为多条消息）
SPLIT_MARKER = "---split---"

# 重试标记（consumer 侧检测并丢弃已累积内容）
RETRY_MARKER = "---retry---"

# 外部图片 URL 检测（模型可能编造 ![image](https://...) 而非调用 generate_image）
EXTERNAL_IMAGE_URL_PATTERN = re.compile(r"!\[.*?\]\((https?://[^\)]+)\)")
CORRECTION_MESSAGE = (
    "你的回复中包含了外部图片URL链接（![image](https://...)），"
    "但这不会显示任何图片。请使用 generate_image 工具重新生成图片。"
)

# 复杂度行为引导
COMPLEXITY_HINTS = {
    Complexity.SIMPLE: "【简洁模式】倾向于直接回答或单次工具调用，快速响应用户。",
    Complexity.COMPLEX: "【深度模式】可以多步推理，充分利用工具收集信息后再综合回答。",
    Complexity.SUPER_COMPLEX: "【研究模式】这是一个复杂的研究任务，可以进行深入分析和多轮工具调用。",
}


async def stream_chat(
    message_id: str, session_id: str | None = None
) -> AsyncGenerator[str, None]:
    """主聊天流式响应入口

    Args:
        message_id: 触发消息的 ID
        session_id: 会话追踪 ID（由 main-server 生成）

    Yields:
        str: 原始 token 文本片段
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
                yield "抱歉，未找到相关消息记录"
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
                    yield GUARD_REJECT_MESSAGE
                    return

                complexity_result = pre_result["complexity_result"]
                complexity = (
                    complexity_result.complexity
                    if complexity_result
                    else Complexity.SIMPLE
                )
                logger.info(f"复杂度路由: complexity={complexity.value}")

                async for text in _build_and_stream(
                    message_id, complexity, gray_config, request_id
                ):
                    yield text
            else:
                # === 并行模式：pre 在后台运行，主模型同时流式生成 ===
                logger.info(f"并行模式启动: message_id={message_id}")
                raw_stream = _build_and_stream(
                    message_id, Complexity.SIMPLE, gray_config, request_id
                )

                async for text in _buffer_until_pre(raw_stream, pre_task, message_id):
                    yield text


_STREAM_END = object()


async def _buffer_until_pre(
    raw_stream: AsyncGenerator[str, None],
    pre_task: asyncio.Task,
    message_id: str,
) -> AsyncGenerator[str, None]:
    """用 pre_task 结果守护一个原始 token 流。

    使用 Queue + asyncio.wait 实现 race：pre 完成后立即响应，
    不再被动等待下一个 token 到达才检查。
    """
    buffer: list[str] = []
    q: asyncio.Queue = asyncio.Queue()

    async def _drain_stream():
        try:
            async for text in raw_stream:
                await q.put(text)
        except Exception as e:
            await q.put(e)
        finally:
            await q.put(_STREAM_END)

    drain_task = asyncio.create_task(_drain_stream())

    try:
        # Phase 1: Race pre vs stream tokens
        while not pre_task.done():
            get_task = asyncio.ensure_future(q.get())
            done, _ = await asyncio.wait(
                {get_task, pre_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if pre_task in done:
                pre_result = pre_task.result()
                if pre_result["is_blocked"]:
                    logger.info(
                        f"并行模式拦截: message_id={message_id}, "
                        f"reason={pre_result['block_reason']}"
                    )
                    get_task.cancel()
                    yield GUARD_REJECT_MESSAGE
                    return
                # pre passed → flush buffer
                for b in buffer:
                    yield b
                buffer.clear()
                # Await pending get
                item = await get_task
                if isinstance(item, Exception):
                    raise item
                if item is _STREAM_END:
                    return
                yield item
                break  # → Phase 2

            # Token arrived, pre still running
            item = await get_task
            if isinstance(item, Exception):
                raise item
            if item is _STREAM_END:
                # Stream ended before pre → await pre
                try:
                    pre_result = await pre_task
                except Exception as e:
                    logger.error(f"pre_task 异常: {e}")
                    for b in buffer:
                        yield b
                    return
                if pre_result["is_blocked"]:
                    logger.info(
                        f"并行模式拦截（流结束后）: message_id={message_id}, "
                        f"reason={pre_result['block_reason']}"
                    )
                    yield GUARD_REJECT_MESSAGE
                    return
                for b in buffer:
                    yield b
                return
            buffer.append(item)

        # Edge: pre done between loop iterations
        if buffer:
            pre_result = pre_task.result()
            if pre_result["is_blocked"]:
                logger.info(
                    f"并行模式拦截: message_id={message_id}, "
                    f"reason={pre_result['block_reason']}"
                )
                yield GUARD_REJECT_MESSAGE
                return
            for b in buffer:
                yield b
            buffer.clear()

        # Phase 2: Pre passed, stream directly from queue
        while True:
            item = await q.get()
            if isinstance(item, Exception):
                raise item
            if item is _STREAM_END:
                return
            yield item

    finally:
        if not drain_task.done():
            drain_task.cancel()


async def _build_and_stream(
    message_id: str,
    complexity: Complexity,
    gray_config: dict,
    session_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """构建 agent + 上下文，执行流式生成（两种模式共用）"""
    # 构建 prompt 变量（注入复杂度引导）
    prompt_vars = {
        "complexity_hint": COMPLEXITY_HINTS.get(complexity, ""),
        "user_context": "",
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
        chat_name,
        chain_user_ids,
    ) = await build_chat_context(message_id)

    if not messages:
        logger.warning(f"No results found for message_id: {message_id}")
        yield "抱歉，未找到相关消息记录"
        return

    # 构建 user_context
    context_lines: list[str] = []
    if chat_type == "p2p":
        if trigger_username:
            context_lines.append(f"你正在和 {trigger_username} 私聊。")
    else:  # group
        if chat_name:
            context_lines.append(f"你在群聊「{chat_name}」中。")
        if trigger_username:
            context_lines.append(
                f"需要回复 {trigger_username} 的消息（消息中用 ⭐ 标记）。"
            )

    # 群聊注入日记记忆 + 人物印象
    if chat_type != "p2p":
        try:
            diary_text = await build_diary_context(chat_id)
            if diary_text:
                context_lines.append("\n---\n你最近的日记：")
                context_lines.append(diary_text)
        except Exception as e:
            logger.error(f"Failed to build diary context: {e}")

        try:
            impression_text = await build_impression_context(
                chat_id, chain_user_ids
            )
            if impression_text:
                context_lines.append("\n---\n" + impression_text)
        except Exception as e:
            logger.error(f"Failed to build impression context: {e}")

    prompt_vars["user_context"] = "\n".join(context_lines)

    full_content = ""
    has_text_in_current_turn = False

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

                if finish_reason == "content_filter":
                    yield "小尾有点不想讨论这个话题呢~"
                    return
                if finish_reason == "length":
                    yield "(后续内容被截断)"
                    return

                if token.text:
                    has_text_in_current_turn = True
                    full_content += token.text
                    yield token.text

                # text → tool call 边界，注入分隔符
                if token.tool_call_chunks and has_text_in_current_turn:
                    yield SPLIT_MARKER
                    has_text_in_current_turn = False

            elif isinstance(token, ToolMessage):
                has_text_in_current_turn = False

        # 检测外部图片 URL，注入纠错消息重试一次
        if EXTERNAL_IMAGE_URL_PATTERN.search(full_content):
            logger.warning(
                "检测到外部图片URL，注入纠错消息重试: %s", message_id
            )
            yield RETRY_MARKER

            retry_messages = messages + [
                AIMessage(content=full_content),
                HumanMessage(content=CORRECTION_MESSAGE),
            ]
            full_content = ""
            async for token in agent.stream(
                retry_messages,
                context=AgentContext(
                    message=MessageContext(
                        message_id=message_id, chat_id=chat_id
                    ),
                    media=MediaContext(image_urls=image_urls or []),
                    features=FeatureFlags(flags=gray_config or {}),
                ),
                prompt_vars=prompt_vars,
            ):
                if isinstance(token, AIMessageChunk):
                    finish_reason = token.response_metadata.get(
                        "finish_reason"
                    )
                    if finish_reason == "content_filter":
                        yield "小尾有点不想讨论这个话题呢~"
                        return
                    if finish_reason == "length":
                        yield "(后续内容被截断)"
                        return
                    if token.text:
                        full_content += token.text
                        yield token.text

        # Fire-and-forget: publish to post safety check queue
        if full_content and session_id:
            asyncio.create_task(
                _publish_post_check(session_id, full_content, chat_id, message_id)
            )

    except Exception as e:
        import traceback

        logger.error(f"stream_chat error: {str(e)}\n{traceback.format_exc()}")
        yield "赤尾好像遇到了一些问题呢QAQ"


async def _publish_post_check(
    session_id: str,
    response_text: str,
    chat_id: str,
    trigger_message_id: str,
) -> None:
    """发布 post safety check 消息到 RabbitMQ"""
    try:
        from app.clients.rabbitmq import SAFETY_CHECK, RabbitMQClient
        from app.utils.middlewares.trace import get_lane

        client = RabbitMQClient.get_instance()
        await client.publish(
            SAFETY_CHECK,
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
