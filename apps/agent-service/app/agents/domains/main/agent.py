"""主聊天 Agent"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator

from langchain.messages import AIMessageChunk, ToolMessage
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
from app.agents.graphs.pre import run_pre
from app.orm.crud import get_gray_config, get_message_content
from app.services.memory_context import build_inner_context
from app.services.bot_context import BotContext
from app.middleware.chat_metrics import CHAT_PIPELINE_DURATION, CHAT_TOKENS
from app.utils.content_parser import parse_content
from app.utils.middlewares.trace import header_vars

logger = logging.getLogger(__name__)

async def _get_guard_message(bot_name: str) -> str:
    """获取 guard 拒绝消息（bot 专属，fallback 为通用消息）"""
    try:
        from app.orm.crud import get_bot_persona
        persona = await get_bot_persona(bot_name)
        if persona and persona.error_messages:
            return persona.error_messages.get("guard", "不想讨论这个话题呢~")
    except Exception as e:
        logger.warning(f"Failed to get guard message for bot={bot_name}: {e}")
    return "不想讨论这个话题呢~"

# 分段标记（consumer 侧检测并拆分为多条消息）
SPLIT_MARKER = "---split---"



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
            t_entry = time.monotonic()
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
            CHAT_PIPELINE_DURATION.labels(stage="prep").observe(time.monotonic() - t_entry)
            pre_blocking = gray_config.get("pre_blocking", "false")

            # 获取 bot 专属 guard 消息
            bot_name = header_vars["app_name"].get() or ""
            guard_message = await _get_guard_message(bot_name)

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
                    yield guard_message
                    return

                async for text in _build_and_stream(
                    message_id, gray_config, request_id
                ):
                    yield text
            else:
                # === 并行模式：pre 在后台运行，主模型同时流式生成 ===
                logger.info(f"并行模式启动: message_id={message_id}")
                raw_stream = _build_and_stream(
                    message_id, gray_config, request_id
                )

                async for text in _buffer_until_pre(raw_stream, pre_task, message_id, guard_message):
                    yield text


_STREAM_END = object()


async def _buffer_until_pre(
    raw_stream: AsyncGenerator[str, None],
    pre_task: asyncio.Task,
    message_id: str,
    guard_message: str = "不想讨论这个话题呢~",
) -> AsyncGenerator[str, None]:
    """用 pre_task 结果守护一个原始 token 流。

    使用 Queue + asyncio.wait 实现 race：pre 完成后立即响应，
    不再被动等待下一个 token 到达才检查。
    """
    t_buf_start = time.monotonic()
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
                pre_dur = time.monotonic() - t_buf_start
                CHAT_PIPELINE_DURATION.labels(stage="pre_safety").observe(pre_dur)
                logger.info(
                    "pre_safety_done",
                    extra={
                        "event": "pre_safety_done",
                        "message_id": message_id,
                        "duration_ms": round(pre_dur * 1000),
                        "blocked": pre_result["is_blocked"],
                        "buffered": len(buffer),
                    },
                )
                if pre_result["is_blocked"]:
                    logger.info(
                        f"并行模式拦截: message_id={message_id}, "
                        f"reason={pre_result['block_reason']}"
                    )
                    get_task.cancel()
                    yield guard_message
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
                pre_dur = time.monotonic() - t_buf_start
                CHAT_PIPELINE_DURATION.labels(stage="pre_safety").observe(pre_dur)
                if pre_result["is_blocked"]:
                    logger.info(
                        f"并行模式拦截（流结束后）: message_id={message_id}, "
                        f"reason={pre_result['block_reason']}"
                    )
                    yield guard_message
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
                yield guard_message
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
    gray_config: dict,
    session_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """构建 agent + 上下文，执行流式生成（两种模式共用）"""
    t_build_start = time.monotonic()
    from app.skills.registry import SkillRegistry

    # 获取当前 bot_name
    bot_name = header_vars["app_name"].get() or ""

    prompt_vars = {
        "complexity_hint": "",
        "inner_context": "",
        "available_skills": SkillRegistry.list_descriptions(),
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
        image_registry,
        chat_id,
        trigger_username,
        chat_type,
        trigger_user_id,
        chat_name,
        chain_user_ids,
    ) = await build_chat_context(message_id, current_bot_name=bot_name)
    CHAT_PIPELINE_DURATION.labels(stage="context_build").observe(time.monotonic() - t_build_start)

    # 创建并加载 BotContext
    bot_ctx = BotContext(chat_id=chat_id, bot_name=bot_name, chat_type=chat_type)
    await bot_ctx.load()

    if not messages:
        logger.warning(f"No results found for message_id: {message_id}")
        yield "抱歉，未找到相关消息记录"
        return

    # 注入 bot identity
    prompt_vars["identity"] = bot_ctx.get_identity()

    # 构建统一 inner_context（场景 + 状态 + 印象 + 引导语）
    try:
        from app.agents.domains.main.context_builder import (
            _is_proactive_var,
            _proactive_stimulus_var,
        )
        prompt_vars["inner_context"] = await build_inner_context(
            chat_id=chat_id,
            chat_type=chat_type,
            user_ids=chain_user_ids,
            trigger_user_id=trigger_user_id,
            trigger_username=trigger_username,
            chat_name=chat_name,
            is_proactive=_is_proactive_var.get(False),
            proactive_stimulus=_proactive_stimulus_var.get(""),
            bot_name=bot_name,
        )
    except Exception as e:
        logger.error(f"Failed to build inner context: {e}")

    # 动态 reply-style（漂移生成的行为示例，fallback 静态示例）
    prompt_vars["reply_style"] = bot_ctx.reply_style

    full_content = ""
    has_text_in_current_turn = False

    try:
        t_agent_start = time.monotonic()
        agent_token_count = 0
        tool_call_count = 0
        async for token in agent.stream(
            messages,
            context=AgentContext(
                message=MessageContext(message_id=message_id, chat_id=chat_id),
                media=MediaContext(registry=image_registry),
                features=FeatureFlags(flags=gray_config or {}),
            ),
            prompt_vars=prompt_vars,
        ):
            if isinstance(token, AIMessageChunk):
                finish_reason = token.response_metadata.get("finish_reason")

                if finish_reason == "content_filter":
                    yield bot_ctx.get_error_message("content_filter")
                    return
                if finish_reason == "length":
                    yield "(后续内容被截断)"
                    return

                if token.text:
                    has_text_in_current_turn = True
                    agent_token_count += 1
                    full_content += token.text
                    yield token.text

                # text → tool call 边界，注入分隔符
                if token.tool_call_chunks and has_text_in_current_turn:
                    yield SPLIT_MARKER
                    has_text_in_current_turn = False

            elif isinstance(token, ToolMessage):
                tool_call_count += 1
                has_text_in_current_turn = False

        agent_dur = time.monotonic() - t_agent_start
        CHAT_PIPELINE_DURATION.labels(stage="agent_stream").observe(agent_dur)
        CHAT_TOKENS.labels(type="text").inc(agent_token_count)
        CHAT_TOKENS.labels(type="tool_call").inc(tool_call_count)
        logger.info(
            "agent_stream_done",
            extra={
                "event": "agent_stream_done",
                "session_id": session_id,
                "context_ms": round((t_agent_start - t_build_start) * 1000),
                "agent_ms": round(agent_dur * 1000),
                "tokens": agent_token_count,
                "tools": tool_call_count,
                "model": model_id,
            },
        )
        # Fire-and-forget: publish to post safety check queue
        if full_content and session_id:
            asyncio.create_task(
                _publish_post_check(session_id, full_content, chat_id, message_id)
            )
        # Fire-and-forget: trigger identity drift
        if full_content:
            try:
                from app.services.identity_drift import IdentityDriftManager

                asyncio.create_task(
                    IdentityDriftManager.get_instance().on_event(chat_id, bot_name)
                )
            except Exception as e:
                logger.warning(f"Identity drift trigger failed: {e}")

    except Exception as e:
        import traceback

        logger.error(f"stream_chat error: {str(e)}\n{traceback.format_exc()}")
        yield bot_ctx.get_error_message("error")


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
