"""Fire-and-forget post-processing after stream completion.

Triggers:
  1. Post safety check — publish to RabbitMQ audit queue
  2. Identity drift — voice regeneration (debounced)
  3. Afterthought — conversation fragment generation (debounced)
"""

from __future__ import annotations

import asyncio
import logging

from app.domain.memory_triggers import AfterthoughtTrigger, DriftTrigger
from app.domain.safety import PostSafetyRequest
from app.memory._persona import load_persona
from app.runtime.data import Data
from app.runtime.emit import emit

logger = logging.getLogger(__name__)


async def fetch_guard_message(persona_or_bot: str) -> str:
    """Fetch persona-specific guard rejection message, with fallback."""
    try:
        pc = await load_persona(persona_or_bot)
        if pc.error_messages:
            return pc.error_messages.get("guard", "不想讨论这个话题呢~")
    except Exception as e:
        logger.warning("Failed to get guard message for %s: %s", persona_or_bot, e)
    return "不想讨论这个话题呢~"


async def _publish_post_check(
    session_id: str,
    response_text: str,
    chat_id: str,
    trigger_message_id: str,
) -> None:
    """Emit PostSafetyRequest into the dataflow graph.

    The wire ``wire(PostSafetyRequest).to(run_post_safety).durable()`` in
    ``app/wiring/safety.py`` queues the request and the durable consumer
    bound on agent-service runs the audit.
    """
    try:
        await emit(PostSafetyRequest(
            session_id=session_id,
            trigger_message_id=trigger_message_id,
            chat_id=chat_id,
            response_text=response_text,
        ))
        logger.info("Emitted PostSafetyRequest: session_id=%s", session_id)
    except Exception as e:
        logger.error("Failed to emit PostSafetyRequest: %s", e)


async def _emit_memory_trigger(trigger: Data) -> None:
    """Fire-and-forget memory trigger emit. Failures are logged, not raised
    (post_actions 调用方语义就是 fire-and-forget；emit 内部任何异常都不该
    污染聊天主链路；reviewer round-1 M6)."""
    try:
        await emit(trigger)
    except Exception:
        logger.exception(
            "failed to emit memory trigger %s: chat_id=%s persona_id=%s",
            type(trigger).__name__,
            getattr(trigger, "chat_id", None),
            getattr(trigger, "persona_id", None),
        )


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

    # 2. Identity drift (debounced voice regeneration via dataflow)
    asyncio.create_task(_emit_memory_trigger(
        DriftTrigger(chat_id=chat_id, persona_id=persona_id)
    ))

    # 3. Afterthought (conversation fragment generation via dataflow)
    asyncio.create_task(_emit_memory_trigger(
        AfterthoughtTrigger(chat_id=chat_id, persona_id=persona_id)
    ))
