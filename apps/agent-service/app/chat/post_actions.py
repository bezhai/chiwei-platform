"""Fire-and-forget post-processing after stream completion.

Triggers:
  1. Post safety check — publish to RabbitMQ audit queue
  2. Identity drift — voice regeneration (debounced)
  3. Afterthought — conversation fragment generation (debounced)
"""

from __future__ import annotations

import asyncio
import logging

from app.data.queries import find_persona
from app.data.session import get_session
from app.infra.rabbitmq import SAFETY_CHECK, mq
from app.utils.middlewares.trace import get_lane

logger = logging.getLogger(__name__)


async def fetch_guard_message(persona_or_bot: str) -> str:
    """Fetch persona-specific guard rejection message, with fallback."""
    try:
        async with get_session() as s:
            persona = await find_persona(s, persona_or_bot)
        if persona and persona.error_messages:
            return persona.error_messages.get("guard", "不想讨论这个话题呢~")
    except Exception as e:
        logger.warning("Failed to get guard message for %s: %s", persona_or_bot, e)
    return "不想讨论这个话题呢~"


async def _publish_post_check(
    session_id: str,
    response_text: str,
    chat_id: str,
    trigger_message_id: str,
) -> None:
    """Publish post safety check payload to RabbitMQ."""
    try:
        await mq.publish(
            SAFETY_CHECK,
            {
                "session_id": session_id,
                "response_text": response_text,
                "chat_id": chat_id,
                "trigger_message_id": trigger_message_id,
                "lane": get_lane(),
            },
        )
        logger.info("Published post safety check: session_id=%s", session_id)
    except Exception as e:
        logger.error("Failed to publish post safety check: %s", e)


def schedule_post_actions(
    full_content: str,
    session_id: str | None,
    chat_id: str,
    message_id: str,
    persona_id: str,
) -> None:
    """Schedule all fire-and-forget post-processing tasks.

    Called after the main stream completes. All tasks are non-blocking.
    """
    if not full_content:
        return

    # 1. Post safety check (RabbitMQ)
    if session_id:
        asyncio.create_task(
            _publish_post_check(session_id, full_content, chat_id, message_id)
        )

    # 2. Identity drift (debounced voice regeneration)
    try:
        from app.memory.drift import drift

        asyncio.create_task(drift.on_event(chat_id, persona_id))
    except Exception as e:
        logger.warning("Identity drift trigger failed: %s", e)

    # 3. Afterthought (conversation fragment generation)
    try:
        from app.memory.afterthought import afterthought

        asyncio.create_task(afterthought.on_event(chat_id, persona_id))
    except Exception as e:
        logger.warning("Afterthought trigger failed: %s", e)
