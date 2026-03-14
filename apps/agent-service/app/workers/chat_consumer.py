"""Chat request MQ consumer

消费 chat_request queue，流式执行 agent 聊天，
检测 ---split--- 分隔符实时切分发送到 chat_response queue。
"""

import json
import logging
import traceback

from aio_pika.abc import AbstractIncomingMessage

from app.agents import stream_chat
from app.clients.rabbitmq import (
    CHAT_REQUEST,
    CHAT_RESPONSE,
    RabbitMQClient,
    _current_lane,
    _lane_queue,
)
from app.utils.middlewares.trace import header_vars

logger = logging.getLogger(__name__)

SPLIT_MARKER = "---split---"
RETRY_MARKER = "---retry---"
MAX_MESSAGES = 4


async def handle_chat_request(message: AbstractIncomingMessage) -> None:
    """消费 chat_request queue 中的消息，流式切分发送"""
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
            sent_length = 0  # 已发送内容的长度
            messages_sent = 0  # 已发送消息条数
            full_content = ""  # 累积的完整内容

            async for text in stream_chat(message_id, session_id=session_id):
                if not text:
                    continue
                if text == RETRY_MARKER:
                    # 外部图片URL检测触发重试，丢弃已累积内容
                    full_content = ""
                    sent_length = 0
                    messages_sent = 0
                    continue
                full_content += text

                # 检测分隔符，逐段发送
                pending = full_content[sent_length:]
                while SPLIT_MARKER in pending and messages_sent < MAX_MESSAGES - 1:
                    idx = pending.index(SPLIT_MARKER)
                    part = pending[:idx].strip()
                    if part:
                        await client.publish(
                            CHAT_RESPONSE,
                            {
                                **base_response,
                                "content": part,
                                "status": "success",
                                "part_index": messages_sent,
                            },
                            lane=lane,
                        )
                        messages_sent += 1
                        logger.info(
                            "Chat response part %d published: session_id=%s",
                            messages_sent - 1,
                            session_id,
                        )
                    sent_length += idx + len(SPLIT_MARKER)
                    pending = full_content[sent_length:]

            # 流结束，发送剩余内容（去掉残留的 split marker）
            remaining = full_content[sent_length:].replace(SPLIT_MARKER, "").strip()
            # 全量文本（去掉所有 split marker），用于数据库存储
            clean_full = full_content.replace(SPLIT_MARKER, "\n\n").strip()

            if remaining or messages_sent == 0:
                await client.publish(
                    CHAT_RESPONSE,
                    {
                        **base_response,
                        "content": remaining or full_content,
                        "full_content": clean_full,
                        "status": "success",
                        "part_index": messages_sent,
                        "is_last": True,
                    },
                    lane=lane,
                )
            else:
                # split marker 后没有内容，仍需通知 worker 结束
                await client.publish(
                    CHAT_RESPONSE,
                    {
                        **base_response,
                        "content": "",
                        "full_content": clean_full,
                        "status": "success",
                        "part_index": messages_sent,
                        "is_last": True,
                    },
                    lane=lane,
                )

            logger.info(
                "Chat response final part %d published: session_id=%s",
                messages_sent,
                session_id,
            )

        except Exception as e:
            logger.error(
                "Chat request failed: session_id=%s, error=%s\n%s",
                session_id,
                str(e),
                traceback.format_exc(),
            )
            await client.publish(
                CHAT_RESPONSE,
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
    queue = _lane_queue(CHAT_REQUEST.queue, lane)
    await client.consume(queue, handle_chat_request)
    logger.info("Chat request consumer started (queue=%s)", queue)
