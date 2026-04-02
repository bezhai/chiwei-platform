"""Proactive eval MQ consumer

消费 proactive_eval 队列，将消息事件转发给 ProactiveManager 做 debounce + 判断。
"""

import json
import logging

from aio_pika.abc import AbstractIncomingMessage

from app.clients.rabbitmq import (
    PROACTIVE_EVAL,
    RabbitMQClient,
    _current_lane,
    _lane_queue,
)
from app.workers.proactive_manager import ProactiveManager

logger = logging.getLogger(__name__)


async def handle_proactive_event(message: AbstractIncomingMessage) -> None:
    """消费 proactive_eval 队列中的消息"""
    async with message.process(requeue=False):
        body = json.loads(message.body)
        chat_id = body.get("chat_id")
        if chat_id:
            manager = ProactiveManager.get_instance()
            await manager.on_event(chat_id)


async def start_proactive_consumer() -> None:
    """启动 proactive eval consumer"""
    client = RabbitMQClient.get_instance()
    await client.connect()
    await client.declare_topology()
    lane = _current_lane()
    queue = _lane_queue(PROACTIVE_EVAL.queue, lane)
    await client.consume(queue, handle_proactive_event)
    logger.info("Proactive eval consumer started (queue=%s)", queue)
