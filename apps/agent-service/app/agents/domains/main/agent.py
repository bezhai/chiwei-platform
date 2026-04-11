"""主聊天 Agent — 编排器

薄编排层：按顺序调用 safety_race / stream_handler / post_actions，
自身不包含业务逻辑。
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator

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
from app.agents.domains.main.post_actions import (
    get_guard_message,
    schedule_post_actions,
)
from app.agents.domains.main.safety_race import buffer_until_pre
from app.agents.domains.main.stream_handler import (
    StreamState,
    handle_token,
    is_content_filter,
    is_length_truncated,
)
from app.agents.domains.main.tools import ALL_TOOLS
from app.agents.graphs.pre import run_pre
from app.middleware.chat_metrics import CHAT_PIPELINE_DURATION, CHAT_TOKENS
from app.orm.crud import get_gray_config, get_message_content
from app.services.bot_context import BotContext
from app.services.memory_context import build_inner_context
from app.services.content_parser import parse_content
from app.utils.middlewares.trace import header_vars

logger = logging.getLogger(__name__)


async def stream_chat(
    message_id: str, session_id: str | None = None, persona_id: str | None = None,
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

            # 获取 guard 消息：优先用 persona_id，fallback header_vars
            effective_persona = persona_id or header_vars["app_name"].get() or ""
            guard_message = await get_guard_message(effective_persona)

            # 3. 启动 pre task（create_task 复制当前 context，继承父 trace）
            pre_task = asyncio.create_task(run_pre(parsed.render(), persona_id=effective_persona))

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
                    message_id, gray_config, request_id, persona_id=persona_id
                ):
                    yield text
            else:
                # === 并行模式：pre 在后台运行，主模型同时流式生成 ===
                logger.info(f"并行模式启动: message_id={message_id}")
                raw_stream = _build_and_stream(
                    message_id, gray_config, request_id, persona_id=persona_id
                )

                async for text in buffer_until_pre(raw_stream, pre_task, message_id, guard_message):
                    yield text


async def _build_and_stream(
    message_id: str,
    gray_config: dict,
    session_id: str | None = None,
    persona_id: str | None = None,
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
    ) = await build_chat_context(message_id, current_persona_id=persona_id or "")
    CHAT_PIPELINE_DURATION.labels(stage="context_build").observe(time.monotonic() - t_build_start)

    # 创建并加载 BotContext
    if persona_id:
        bot_ctx = await BotContext.from_persona_id(
            chat_id=chat_id, persona_id=persona_id, chat_type=chat_type
        )
    else:
        # 兼容：没有 persona_id 时走老路径
        bot_ctx = BotContext(chat_id=chat_id, bot_name=bot_name, chat_type=chat_type)
        await bot_ctx.load()

    if not messages:
        logger.warning(f"No results found for message_id: {message_id}")
        yield "抱歉，未找到相关消息记录"
        return

    # 注入 bot identity + 外貌
    prompt_vars["identity"] = bot_ctx.get_identity()
    prompt_vars["appearance"] = bot_ctx.get_appearance_detail()

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
            persona_id=bot_ctx.persona_id,
        )
    except Exception as e:
        logger.error(f"Failed to build inner context: {e}")

    # 统一 voice（内心独白 + 风格示例，一次生成确保一致）
    prompt_vars["voice_content"] = bot_ctx.voice_content

    state = StreamState()

    try:
        t_agent_start = time.monotonic()
        async for token in agent.stream(
            messages,
            context=AgentContext(
                message=MessageContext(message_id=message_id, chat_id=chat_id),
                media=MediaContext(registry=image_registry),
                features=FeatureFlags(flags=gray_config or {}),
            ),
            prompt_vars=prompt_vars,
        ):
            result = handle_token(token, state)

            if is_content_filter(result):
                yield bot_ctx.get_error_message("content_filter")
                return
            if is_length_truncated(result):
                yield "(后续内容被截断)"
                return

            for text in result:
                if text is not None:
                    yield text

        agent_dur = time.monotonic() - t_agent_start
        CHAT_PIPELINE_DURATION.labels(stage="agent_stream").observe(agent_dur)
        CHAT_TOKENS.labels(type="text").inc(state.agent_token_count)
        CHAT_TOKENS.labels(type="tool_call").inc(state.tool_call_count)
        logger.info(
            "agent_stream_done",
            extra={
                "event": "agent_stream_done",
                "session_id": session_id,
                "context_ms": round((t_agent_start - t_build_start) * 1000),
                "agent_ms": round(agent_dur * 1000),
                "tokens": state.agent_token_count,
                "tools": state.tool_call_count,
                "model": model_id,
            },
        )

        schedule_post_actions(
            full_content=state.full_content,
            session_id=session_id,
            chat_id=chat_id,
            message_id=message_id,
            persona_id=bot_ctx.persona_id,
        )

    except Exception as e:
        import traceback

        logger.error(f"stream_chat error: {str(e)}\n{traceback.format_exc()}")
        yield bot_ctx.get_error_message("error")
