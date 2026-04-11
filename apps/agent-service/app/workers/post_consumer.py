"""Post-processing MQ consumer

消费 safety_check queue，执行输出安全检测，
不安全时发布 recall 消息到 main-server worker，
通过时更新 agent_responses.safety_status = 'passed'。
"""

import json
import logging
from datetime import UTC, datetime

from aio_pika.abc import AbstractIncomingMessage

from app.agents.graphs.post import run_post_safety
from app.clients.rabbitmq import (
    RECALL,
    SAFETY_CHECK,
    RabbitMQClient,
    _current_lane,
    _lane_queue,
)
from app.orm.crud.message import update_safety_status
from app.workers.error_handling import mq_error_handler

logger = logging.getLogger(__name__)


@mq_error_handler()
async def handle_safety_check(message: AbstractIncomingMessage) -> None:
    """消费 safety_check queue 中的消息"""
    async with message.process(requeue=False):
        body = json.loads(message.body)
        session_id = body.get("session_id")
        response_text = body.get("response_text", "")
        chat_id = body.get("chat_id")
        trigger_message_id = body.get("trigger_message_id")
        lane = body.get("lane")  # 从消息 payload 中读取泳道
        if lane:
            from app.utils.middlewares.trace import header_vars

            header_vars["lane"].set(lane)

        logger.info("Post safety check: session_id=%s, lane=%s", session_id, lane)

        result = await run_post_safety(response_text)
        checked_at = datetime.now(UTC).isoformat()

        if result.blocked:
            logger.warning(
                "Post safety blocked: session_id=%s, reason=%s",
                session_id,
                result.reason,
            )
            client = RabbitMQClient.get_instance()
            await client.publish(
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
            await update_safety_status(
                session_id,
                "passed",
                {"checked_at": checked_at},
            )


async def start_post_consumer() -> None:
    """启动 post processing consumer"""
    client = RabbitMQClient.get_instance()
    await client.connect()
    await client.declare_topology()
    lane = _current_lane()
    queue = _lane_queue(SAFETY_CHECK.queue, lane)
    await client.consume(queue, handle_safety_check)
    logger.info("Post safety consumer started (queue=%s)", queue)
