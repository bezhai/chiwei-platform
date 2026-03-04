"""Chat request MQ consumer

消费 chat_request queue，执行 agent 聊天，
将完整回复发布到 chat_response queue。
"""

import json
import logging
import traceback

from aio_pika.abc import AbstractIncomingMessage

from app.clients.rabbitmq import (
    QUEUE_CHAT_REQUEST,
    RK_CHAT_RESPONSE,
    RabbitMQClient,
    _current_lane,
    _lane_queue,
)
from app.services.chat_service import process_chat
from app.utils.middlewares.trace import header_vars

logger = logging.getLogger(__name__)


async def handle_chat_request(message: AbstractIncomingMessage) -> None:
    """消费 chat_request queue 中的消息"""
    async with message.process(requeue=False):
        body = json.loads(message.body)
        session_id = body.get("session_id")
        message_id = body.get("message_id")
        chat_id = body.get("chat_id")
        is_p2p = body.get("is_p2p", False)
        root_id = body.get("root_id")
        user_id = body.get("user_id")
        lane = body.get("lane")
        bot_name = body.get("bot_name")

        # MQ consumer 不走 HTTP 中间件，手动注入 contextvars
        if bot_name:
            header_vars["app_name"].set(bot_name)
        if lane:
            header_vars["lane"].set(lane)

        logger.info(
            "Chat request received: session_id=%s, message_id=%s, lane=%s, bot_name=%s",
            session_id,
            message_id,
            lane,
            bot_name,
        )

        client = RabbitMQClient.get_instance()

        # 构建 response 基础字段
        base_response = {
            "session_id": session_id,
            "message_id": message_id,
            "chat_id": chat_id,
            "is_p2p": is_p2p,
            "root_id": root_id,
            "user_id": user_id,
            "lane": lane,
        }

        try:
            content = await process_chat(message_id, session_id=session_id)

            await client.publish(
                RK_CHAT_RESPONSE,
                {
                    **base_response,
                    "content": content,
                    "status": "success",
                },
                lane=lane,
            )
            logger.info("Chat response published: session_id=%s", session_id)

        except Exception as e:
            logger.error(
                "Chat request failed: session_id=%s, error=%s\n%s",
                session_id,
                str(e),
                traceback.format_exc(),
            )
            await client.publish(
                RK_CHAT_RESPONSE,
                {
                    **base_response,
                    "content": "",
                    "status": "failed",
                    "error": str(e),
                },
                lane=lane,
            )


async def start_chat_consumer() -> None:
    """启动 chat request consumer"""
    client = RabbitMQClient.get_instance()
    await client.connect()
    await client.declare_topology()
    lane = _current_lane()
    queue = _lane_queue(QUEUE_CHAT_REQUEST, lane)
    await client.consume(queue, handle_chat_request)
    logger.info("Chat request consumer started (queue=%s)", queue)
