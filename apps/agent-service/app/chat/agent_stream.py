"""Agent execution helpers used by the chat dataflow node.

``_build_and_stream`` builds the agent context (history, persona, inner
context, voice) and drives ``Agent.stream`` for a single chat request,
yielding decoded text fragments and split markers.

Extracted from the deleted ``app.chat.pipeline`` module during Phase 5a
Task 12: the legacy ``stream_chat`` orchestrator + MQ ``chat_consumer``
were replaced by ``route_chat_node`` / ``chat_node`` (graph), but the
agent-execution helpers remained orthogonal to those orchestration
concerns and are reused verbatim by ``chat_node``.

Public surface (callable from chat_node only):
    _build_and_stream(message_id, gray_config, session_id, persona_id)
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.agent.tools import ALL_TOOLS
from app.api.middleware import CHAT_PIPELINE_DURATION, CHAT_TOKENS, header_vars
from app.chat.context import (
    build_chat_context,
    is_proactive_var,
    proactive_stimulus_var,
)
from app.chat.post_actions import schedule_post_actions
from app.chat.stream import (
    StreamState,
    handle_token,
    is_content_filter,
    is_length_truncated,
)
from app.data.queries import find_latest_reply_style, resolve_persona_id
from app.data.session import get_session
from app.memory._persona import load_persona
from app.memory.context import build_inner_context

logger = logging.getLogger(__name__)

_MAIN_CFG = AgentConfig("main", "main-chat-model", "main")


# ---------------------------------------------------------------------------
# Build context + run agent stream
# ---------------------------------------------------------------------------


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

    cfg = _replace(_MAIN_CFG, model_id=model_id)
    agent = Agent(cfg, tools=ALL_TOOLS)
    state = StreamState()

    try:
        t_agent_start = time.monotonic()
        async for token in agent.stream(
            ctx.messages,
            context=AgentContext(
                message_id=message_id,
                chat_id=ctx.chat_id,
                persona_id=bot_ctx.persona_id,
                image_registry=ctx.image_registry,
                features=dict(gray_config or {}),
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

        logger.error(
            "_build_and_stream error: %s\n%s", e, traceback.format_exc()
        )
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
        if self._persona and self._persona.error_messages:
            return self._persona.error_messages.get(
                kind, f"{self._persona.display_name}遇到了问题QAQ"
            )
        return f"{self.persona_id}遇到了问题QAQ"


async def _load_bot_context(
    persona_id: str | None,
    bot_name: str,
    chat_id: str,
    chat_type: str,
) -> _BotCtx:
    """Load persona data and voice content, return a lightweight context."""
    if persona_id:
        pid = persona_id
    else:
        async with get_session() as s:
            pid = await resolve_persona_id(s, bot_name)

    pc = await load_persona(pid)

    async with get_session() as s:
        voice_content = await find_latest_reply_style(s, pid) or ""

    return _BotCtx(
        persona_id=pid,
        identity=pc.persona_lite,
        appearance=pc.appearance_detail,
        voice_content=voice_content,
        persona=pc,
    )
