"""Fire-and-forget post-processing after stream completion.

Triggers:
  1. Post safety check — publish to RabbitMQ audit queue

（v4 记忆的对话碎片触发链已随旧记忆机器整体删除。）
"""

from __future__ import annotations

import logging

from app.domain.safety import PostSafetyRequest
from app.memory._persona import load_persona
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
    channel: str,
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
            channel=channel,
            trigger_message_id=trigger_message_id,
            chat_id=chat_id,
            response_text=response_text,
        ))
        logger.info("Emitted PostSafetyRequest: session_id=%s", session_id)
    except Exception as e:
        logger.error("Failed to emit PostSafetyRequest: %s", e)


async def schedule_post_actions(
    full_content: str,
    session_id: str | None,
    channel: str,
    chat_id: str,
    message_id: str,
) -> None:
    """Run all fire-and-forget post-processing tasks via dataflow emit.

    Called after the main stream completes; the chat stream chunks have
    already been yielded by this point, so the few-ms cost of awaiting
    the emits sequentially is invisible to the user. Each helper still
    swallows its own exceptions to preserve fire-and-forget semantics.
    """
    if not full_content:
        return

    # Post safety check (durable wire -> run_post_safety)
    if session_id:
        await _publish_post_check(session_id, channel, full_content, chat_id, message_id)
