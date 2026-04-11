"""Main chat pipeline — stream_chat orchestrator.

Thin orchestration layer that wires together:
  1. Message content fetch + v2 parse
  2. Pre-safety check (parallel or blocking)
  3. Context build (history, images, persona, memory)
  4. Agent stream with tools
  5. Stream handling (token counting, content filter, truncation)
  6. Post-actions (safety audit, drift, afterthought)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator

from langfuse import get_client as get_langfuse
from langfuse import propagate_attributes

from app.agent.context import (
    AgentContext,
    FeatureFlags,
    MediaContext,
    MessageContext,
)
from app.agent.core import Agent, AgentConfig
from app.agent.tools import ALL_TOOLS
from app.api.middleware import CHAT_PIPELINE_DURATION, CHAT_TOKENS, header_vars
from app.chat.content_parser import parse_content
from app.chat.context import (
    build_chat_context,
    is_proactive_var,
    proactive_stimulus_var,
)
from app.chat.post_actions import fetch_guard_message, schedule_post_actions
from app.chat.safety import run_pre_check
from app.chat.stream import (
    StreamState,
    handle_token,
    is_content_filter,
    is_length_truncated,
)
from app.data.queries import (
    find_gray_config,
    find_latest_reply_style,
    find_message_content,
    find_persona,
    resolve_persona_id,
)
from app.data.session import get_session
from app.memory.context import build_inner_context

_MAIN_CFG = AgentConfig("main", "main-chat-model", "main")

logger = logging.getLogger(__name__)


async def stream_chat(
    message_id: str,
    session_id: str | None = None,
    persona_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Main streaming chat response entry point.

    Args:
        message_id: trigger message ID
        session_id: session tracking ID (from main-server)
        persona_id: explicit persona override

    Yields:
        Raw text token fragments.
    """
    langfuse = get_langfuse()
    request_id = session_id or str(uuid.uuid4())

    with langfuse.start_as_current_observation(as_type="span", name="chat-request"):
        with propagate_attributes(session_id=request_id):
            t_entry = time.monotonic()

            # 1. Fetch message content
            async with get_session() as s:
                raw_content = await find_message_content(s, message_id)
            if not raw_content:
                logger.warning("No message found for message_id: %s", message_id)
                yield "抱歉，未找到相关消息记录"
                return

            parsed = parse_content(raw_content)

            # 2. Gray config
            async with get_session() as s:
                gray_config = (await find_gray_config(s, message_id)) or {}
            CHAT_PIPELINE_DURATION.labels(stage="prep").observe(
                time.monotonic() - t_entry
            )
            pre_blocking = gray_config.get("pre_blocking", "false")

            # Resolve guard message
            effective_persona = persona_id or header_vars["app_name"].get() or ""
            guard_message = await fetch_guard_message(effective_persona)

            # 3. Pre-safety check
            pre_task = asyncio.create_task(
                run_pre_check(parsed.render(), persona_id=effective_persona)
            )

            if pre_blocking != "false":
                # === Blocking mode: wait for pre before streaming ===
                pre_result = await pre_task
                if pre_result.is_blocked:
                    logger.info(
                        "Message blocked: message_id=%s, reason=%s",
                        message_id,
                        pre_result.block_reason,
                    )
                    yield guard_message
                    return

                async for text in _build_and_stream(
                    message_id, gray_config, request_id, persona_id=persona_id
                ):
                    yield text
            else:
                # === Parallel mode: pre runs in background ===
                logger.info("Parallel mode: message_id=%s", message_id)
                raw_stream = _build_and_stream(
                    message_id, gray_config, request_id, persona_id=persona_id
                )
                async for text in _buffer_until_pre(
                    raw_stream, pre_task, message_id, guard_message
                ):
                    yield text


# ---------------------------------------------------------------------------
# Build context + run agent stream
# ---------------------------------------------------------------------------

_STREAM_END = object()


async def _build_and_stream(
    message_id: str,
    gray_config: dict,
    session_id: str | None = None,
    persona_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Build agent context and execute streaming generation."""
    t_build_start = time.monotonic()

    from app.skills.registry import SkillRegistry

    bot_name = header_vars["app_name"].get() or ""

    prompt_vars: dict[str, str] = {
        "complexity_hint": "",
        "inner_context": "",
        "available_skills": SkillRegistry.list_descriptions(),
    }

    # Resolve model
    model_id = "main-chat-model"
    if gray_config.get("main_model"):
        model_id = str(gray_config["main_model"])

    # Build context
    ctx = await build_chat_context(message_id, current_persona_id=persona_id or "")
    CHAT_PIPELINE_DURATION.labels(stage="context_build").observe(
        time.monotonic() - t_build_start
    )

    if not ctx.messages:
        logger.warning("No results found for message_id: %s", message_id)
        yield "抱歉，未找到相关消息记录"
        return

    # Load persona
    bot_ctx = await _load_bot_context(
        persona_id=persona_id,
        bot_name=bot_name,
        chat_id=ctx.chat_id,
        chat_type=ctx.chat_type,
    )

    # Inject identity + appearance
    prompt_vars["identity"] = bot_ctx.identity
    prompt_vars["appearance"] = bot_ctx.appearance

    # Build inner context (scene + life state + relationship + fragments)
    try:
        prompt_vars["inner_context"] = await build_inner_context(
            chat_id=ctx.chat_id,
            chat_type=ctx.chat_type,
            user_ids=ctx.chain_user_ids,
            trigger_user_id=ctx.trigger_user_id,
            trigger_username=ctx.trigger_username,
            chat_name=ctx.chat_name,
            is_proactive=is_proactive_var.get(False),
            proactive_stimulus=proactive_stimulus_var.get(""),
            persona_id=bot_ctx.persona_id,
        )
    except Exception as e:
        logger.error("Failed to build inner context: %s", e)

    # Voice
    prompt_vars["voice_content"] = bot_ctx.voice_content

    # Create agent and stream
    from dataclasses import replace as _replace

    cfg = _MAIN_CFG if not model_id else _replace(_MAIN_CFG, model_id=model_id)
    agent = Agent(cfg, tools=ALL_TOOLS)
    state = StreamState()

    try:
        t_agent_start = time.monotonic()
        async for token in agent.stream(
            ctx.messages,
            context=AgentContext(
                message=MessageContext(message_id=message_id, chat_id=ctx.chat_id),
                media=MediaContext(registry=ctx.image_registry),
                features=FeatureFlags(flags=gray_config or {}),
            ),
            prompt_vars=prompt_vars,
        ):
            result = handle_token(token, state)

            if is_content_filter(result):
                yield bot_ctx.error_message("content_filter")
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
            chat_id=ctx.chat_id,
            message_id=message_id,
            persona_id=bot_ctx.persona_id,
        )

    except Exception as e:
        import traceback

        logger.error("stream_chat error: %s\n%s", e, traceback.format_exc())
        yield bot_ctx.error_message("error")


# ---------------------------------------------------------------------------
# Bot context loading
# ---------------------------------------------------------------------------


class _BotCtx:
    """Lightweight bot context container (no class hierarchy, just data)."""

    __slots__ = ("persona_id", "identity", "appearance", "voice_content", "_persona")

    def __init__(
        self,
        persona_id: str,
        identity: str,
        appearance: str,
        voice_content: str,
        persona: object | None,
    ) -> None:
        self.persona_id = persona_id
        self.identity = identity
        self.appearance = appearance
        self.voice_content = voice_content
        self._persona = persona

    def error_message(self, kind: str) -> str:
        """Return persona-specific error message."""
        persona = self._persona
        if persona and hasattr(persona, "error_messages") and persona.error_messages:
            return persona.error_messages.get(
                kind, f"{self._display_name()}遇到了问题QAQ"
            )
        return f"{self._display_name()}遇到了问题QAQ"

    def _display_name(self) -> str:
        persona = self._persona
        if persona and hasattr(persona, "display_name") and persona.display_name:
            return persona.display_name
        return self.persona_id


async def _load_bot_context(
    persona_id: str | None,
    bot_name: str,
    chat_id: str,
    chat_type: str,
) -> _BotCtx:
    """Load persona data and voice content, return a lightweight context."""
    async with get_session() as s:
        if persona_id:
            pid = persona_id
        else:
            pid = await resolve_persona_id(s, bot_name)

        persona = await find_persona(s, pid)
        voice_content = await find_latest_reply_style(s, pid) or ""

    identity = persona.persona_lite if persona else ""
    appearance = (
        persona.appearance_detail if persona and persona.appearance_detail else ""
    )

    return _BotCtx(
        persona_id=pid,
        identity=identity,
        appearance=appearance,
        voice_content=voice_content,
        persona=persona,
    )


# ---------------------------------------------------------------------------
# Pre-safety race (parallel mode)
# ---------------------------------------------------------------------------


async def _buffer_until_pre(
    raw_stream: AsyncGenerator[str, None],
    pre_task: asyncio.Task,
    message_id: str,
    guard_message: str = "不想讨论这个话题呢~",
) -> AsyncGenerator[str, None]:
    """Guard a token stream with a pre-safety task.

    Phase 1: buffer tokens while pre runs, using Queue + asyncio.wait for race.
    Phase 2: pre passed, passthrough remaining tokens.
    """
    t_buf_start = time.monotonic()
    buffer: list[str] = []
    q: asyncio.Queue = asyncio.Queue()

    async def _drain_stream():
        try:
            async for text in raw_stream:
                await q.put(text)
        except asyncio.CancelledError:
            logger.warning("_drain_stream cancelled: message_id=%s", message_id)
            raise
        except Exception as e:
            logger.error("_drain_stream error: message_id=%s, error=%s", message_id, e)
            await q.put(e)
        finally:
            try:
                await q.put(_STREAM_END)
            except asyncio.CancelledError:
                q.put_nowait(_STREAM_END)

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
                        "blocked": pre_result.is_blocked,
                        "buffered": len(buffer),
                    },
                )
                if pre_result.is_blocked:
                    logger.info(
                        "Parallel blocked: message_id=%s, reason=%s",
                        message_id,
                        pre_result.block_reason,
                    )
                    get_task.cancel()
                    yield guard_message
                    return
                # Flush buffer
                for b in buffer:
                    yield b
                buffer.clear()
                item = await get_task
                if isinstance(item, Exception):
                    raise item
                if item is _STREAM_END:
                    return
                yield item
                break  # -> Phase 2

            # Token arrived, pre still running
            item = await get_task
            if isinstance(item, Exception):
                raise item
            if item is _STREAM_END:
                # Stream ended before pre -> await pre
                try:
                    pre_result = await pre_task
                except Exception as e:
                    logger.error("pre_task exception: %s", e)
                    for b in buffer:
                        yield b
                    return
                pre_dur = time.monotonic() - t_buf_start
                CHAT_PIPELINE_DURATION.labels(stage="pre_safety").observe(pre_dur)
                if pre_result.is_blocked:
                    logger.info(
                        "Parallel blocked (post-stream): message_id=%s, reason=%s",
                        message_id,
                        pre_result.block_reason,
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
            if pre_result.is_blocked:
                logger.info(
                    "Parallel blocked: message_id=%s, reason=%s",
                    message_id,
                    pre_result.block_reason,
                )
                yield guard_message
                return
            for b in buffer:
                yield b
            buffer.clear()

        # Phase 2: passthrough
        _PHASE2_TIMEOUT = 120
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=_PHASE2_TIMEOUT)
            except TimeoutError:
                logger.error(
                    "_buffer_until_pre phase2 TIMEOUT (%ds): message_id=%s",
                    _PHASE2_TIMEOUT,
                    message_id,
                )
                return
            if isinstance(item, Exception):
                raise item
            if item is _STREAM_END:
                return
            yield item

    finally:
        if not drain_task.done():
            drain_task.cancel()
