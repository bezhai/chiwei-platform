"""Post-processing MQ consumer — safety check on AI responses.

Consumes ``safety_check`` queue, runs ``run_post_check``,
publishes recall on block, updates safety_status on pass.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from aio_pika.abc import AbstractIncomingMessage

from app.api.middleware import header_vars
from app.chat.safety import run_post_check
from app.data.queries import set_safety_status
from app.data.session import get_session
from app.infra.rabbitmq import (
    RECALL,
    SAFETY_CHECK,
    _current_lane,
    _lane_queue,
    mq,
)
from app.workers.common import mq_error_handler

logger = logging.getLogger(__name__)


@mq_error_handler()
async def handle_safety_check(message: AbstractIncomingMessage) -> None:
    """Consume safety_check queue: audit response, recall if blocked."""
    async with message.process(requeue=False):
        body = json.loads(message.body)
        session_id = body.get("session_id")
        response_text = body.get("response_text", "")
        chat_id = body.get("chat_id")
        trigger_message_id = body.get("trigger_message_id")
        lane = body.get("lane")

        if lane:
            header_vars["lane"].set(lane)

        logger.info("Post safety check: session_id=%s, lane=%s", session_id, lane)

        result = await run_post_check(response_text)
        checked_at = datetime.now(UTC).isoformat()

        if result.blocked:
            logger.warning(
                "Post safety blocked: session_id=%s, reason=%s",
                session_id,
                result.reason,
            )
            await mq.publish(
                RECALL,
                {
                    "session_id": session_id,
                    "chat_id": chat_id,
                    "trigger_message_id": trigger_message_id,
                    "reason": result.reason,
                    "detail": result.detail,
                    "lane": lane,
                },
                lane=lane,
            )
        else:
            logger.info("Post safety passed: session_id=%s", session_id)
            async with get_session() as s:
                await set_safety_status(
                    s,
                    session_id,
                    "passed",
                    {"checked_at": checked_at},
                )


async def start_post_consumer() -> None:
    """Connect MQ and start consuming the safety_check queue."""
    await mq.connect()
    await mq.declare_topology()
    lane = _current_lane()
    queue = _lane_queue(SAFETY_CHECK.queue, lane)
    await mq.consume(queue, handle_safety_check)
    logger.info("Post safety consumer started (queue=%s)", queue)
