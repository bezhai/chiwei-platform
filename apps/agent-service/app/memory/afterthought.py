"""Afterthought — conversation experience fragment generation.

Inherits ``DebouncedPipeline``:
  Phase 1: collect messages, debounce 300 s, force flush at 15.
  Phase 2: LLM generates a *conversation*-grain ``ExperienceFragment``.

Each ``(chat_id, persona_id)`` pair is managed independently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from app.agent.core import Agent
from app.data.models import ExperienceFragment
from app.data.queries import (
    find_group_name,
    find_messages_in_range,
    find_username,
    insert_fragment,
)
from app.data.session import get_session
from app.memory._persona import load_persona
from app.memory._timeline import format_timeline
from app.memory.debounce import DebouncedPipeline

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))

DEBOUNCE_SECONDS = 300  # 5 minutes
MAX_BUFFER = 15
LOOKBACK_HOURS = 2


def _text(content) -> str:
    """Extract plain text from an LLM response content value."""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return (content or "").strip()


class _Afterthought(DebouncedPipeline):
    """Two-phase debounced conversation fragment generator."""

    def __init__(self) -> None:
        super().__init__(debounce_seconds=DEBOUNCE_SECONDS, max_buffer=MAX_BUFFER)

    async def process(self, chat_id: str, persona_id: str, event_count: int) -> None:
        await _generate_fragment(chat_id, persona_id)


afterthought = _Afterthought()


# ---------------------------------------------------------------------------
# Core generation logic
# ---------------------------------------------------------------------------


async def _generate_fragment(chat_id: str, persona_id: str) -> None:
    """Generate a conversation-grain experience fragment.

    1. Fetch last 2 hours of messages
    2. Build scene description (group name / p2p partner)
    3. Format timeline
    4. Call LLM to generate fragment content
    5. Persist ExperienceFragment
    6. Fire relationship extraction (non-blocking)
    """
    now = datetime.now(_CST)
    start_ts = int((now - timedelta(hours=LOOKBACK_HOURS)).timestamp() * 1000)
    end_ts = int(now.timestamp() * 1000)

    async with get_session() as s:
        messages = await find_messages_in_range(s, chat_id, start_ts, end_ts)

    if not messages:
        logger.info(
            "[%s] No messages in last %dh for %s, skip",
            persona_id,
            LOOKBACK_HOURS,
            chat_id,
        )
        return

    chat_type = messages[0].chat_type if messages else "group"

    pc = await load_persona(persona_id)
    scene = await _build_scene(chat_id, chat_type, messages)
    timeline = await format_timeline(messages, pc.display_name, tz=_CST)
    if not timeline:
        logger.info("[%s] Empty timeline for %s, skip", persona_id, chat_id)
        return

    result = await Agent("afterthought").run(
        prompt_vars={
            "persona_name": pc.display_name,
            "persona_lite": pc.persona_lite,
            "scene": scene,
            "messages": timeline,
        },
        messages=[HumanMessage(content="生成经历碎片")],
    )
    content = _text(result.content)

    if not content:
        logger.warning(
            "[%s] Afterthought LLM returned empty for %s", persona_id, chat_id
        )
        return

    fragment = ExperienceFragment(
        persona_id=persona_id,
        grain="conversation",
        source_chat_id=chat_id,
        source_type=chat_type,
        time_start=start_ts,
        time_end=end_ts,
        content=content,
        mentioned_entity_ids=[],
    )
    async with get_session() as s:
        await insert_fragment(s, fragment)
    logger.info(
        "[%s] Conversation fragment created for %s: %s...",
        persona_id,
        chat_id,
        content[:60],
    )

    # Relationship extraction (fire-and-forget)
    try:
        from app.memory.relationships import extract_relationship_updates

        unique_user_ids = list(
            {
                m.user_id
                for m in messages
                if m.role == "user" and m.user_id and m.user_id != "__proactive__"
            }
        )
        if unique_user_ids:
            await extract_relationship_updates(
                persona_id=persona_id,
                chat_id=chat_id,
                user_ids=unique_user_ids,
                messages=messages,
            )
    except Exception as e:
        logger.warning(
            "[%s] Relationship extract failed (non-fatal): %s", persona_id, e
        )


async def _build_scene(chat_id: str, chat_type: str, messages: list) -> str:
    """Build scene description for the prompt."""
    if chat_type == "p2p":
        for msg in messages:
            if msg.role == "user" and msg.user_id:
                async with get_session() as s:
                    name = await find_username(s, msg.user_id)
                if name:
                    return f"和{name}的私聊"
        return "一段私聊"

    try:
        async with get_session() as s:
            group_name = await find_group_name(s, chat_id)
        if group_name:
            return f"在「{group_name}」群里"
    except Exception:
        pass
    return "在群里"
