"""Chat request MQ consumer

消费 chat_request queue，流式执行 agent 聊天，
检测 ---split--- 分隔符实时切分发送到 chat_response queue。
"""

import json
import logging
import time
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
from app.middleware.chat_metrics import (
    CHAT_FIRST_TOKEN,
    CHAT_PIPELINE_DURATION,
    CHAT_QUEUE_WAIT,
    CHAT_TOKENS,
)
from app.utils.middlewares.trace import header_vars

logger = logging.getLogger(__name__)

SPLIT_MARKER = "---split---"
MAX_MESSAGES = 4


async def handle_chat_request(message: AbstractIncomingMessage) -> None:
    """消费 chat_request queue 中的消息，流式切分发送"""
    async with message.process(requeue=False):
        t_start = time.monotonic()
        body = json.loads(message.body)
        session_id = body.get("session_id")
        message_id = body.get("message_id")
        chat_id = body.get("chat_id")
        is_p2p = body.get("is_p2p", False)
        root_id = body.get("root_id")
        user_id = body.get("user_id")
        lane = body.get("lane")
        bot_name = body.get("bot_name")
        is_proactive = body.get("is_proactive", False)

        # Measure MQ queue wait time
        queue_wait_ms = 0.0
        enqueued_at = body.get("enqueued_at")
        if enqueued_at:
            queue_wait_s = (time.time() * 1000 - enqueued_at) / 1000
            queue_wait_ms = queue_wait_s * 1000
            CHAT_QUEUE_WAIT.observe(queue_wait_s)

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
            "is_proactive": is_proactive,
            "bot_name": bot_name,
        }

        try:
            sent_length = 0  # 已发送内容的长度
            messages_sent = 0  # 已发送消息条数
            full_content = ""  # 累积的完整内容
            t_first_token: float | None = None
            token_count = 0

            async for text in stream_chat(message_id, session_id=session_id):
                if not text:
                    continue
                if t_first_token is None:
                    t_first_token = time.monotonic()
                token_count += 1
                full_content += text

                # 检测分隔符，逐段发送
                pending = full_content[sent_length:]
                while SPLIT_MARKER in pending and messages_sent < MAX_MESSAGES - 1:
                    idx = pending.index(SPLIT_MARKER)
                    part = pending[:idx].strip()
                    if part:
                        base_response["published_at"] = int(time.time() * 1000)
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

            # 记录流结束时间，观测 TTFT 和 agent_stream 阶段耗时
            t_stream_end = time.monotonic()
            stream_ms = (t_stream_end - t_start) * 1000
            if t_first_token is not None:
                CHAT_FIRST_TOKEN.observe(t_first_token - t_start)
            CHAT_PIPELINE_DURATION.labels(stage="agent_stream").observe(t_stream_end - t_start)
            CHAT_TOKENS.labels(type="text").inc(token_count)

            # 流结束，发送剩余内容（去掉残留的 split marker）
            remaining = full_content[sent_length:].replace(SPLIT_MARKER, "").strip()
            # 全量文本（去掉所有 split marker），用于数据库存储
            clean_full = full_content.replace(SPLIT_MARKER, "\n\n").strip()

            t_publish_start = time.monotonic()
            if remaining or messages_sent == 0:
                base_response["published_at"] = int(time.time() * 1000)
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
                base_response["published_at"] = int(time.time() * 1000)
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

            # 记录 MQ publish 耗时和全链路总时长
            t_end = time.monotonic()
            publish_ms = (t_end - t_publish_start) * 1000
            total_ms = (t_end - t_start) * 1000
            ttft_ms = (t_first_token - t_start) * 1000 if t_first_token is not None else 0.0
            CHAT_PIPELINE_DURATION.labels(stage="mq_publish").observe(t_end - t_publish_start)
            CHAT_PIPELINE_DURATION.labels(stage="total").observe(t_end - t_start)
            logger.info(
                "chat_request_done",
                extra={
                    "event": "chat_request_done",
                    "session_id": session_id,
                    "queue_wait_ms": round(queue_wait_ms),
                    "stream_ms": round(stream_ms),
                    "ttft_ms": round(ttft_ms),
                    "publish_ms": round(publish_ms),
                    "total_ms": round(total_ms),
                    "tokens": token_count,
                    "parts": messages_sent + 1,
                },
            )

            # Piggyback: 回复完后顺手刷一眼群聊（proactive 回复不触发，避免递归）[DISABLED]
            # if not is_proactive:
            #     await _maybe_piggyback_scan()

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


async def _maybe_piggyback_scan() -> None:
    """概率触发一次主动搭话扫描（piggyback 模式）"""
    import random
    try:
        if random.random() > 0.6:  # 40% 概率跳过，即 60% 概率触发
            return
        from app.workers.proactive_scanner import run_proactive_scan
        await run_proactive_scan(source="piggyback")
    except Exception as e:
        logger.warning(f"piggyback scan failed: {e}")


async def start_chat_consumer() -> None:
    """启动 chat request consumer"""
    client = RabbitMQClient.get_instance()
    await client.connect()
    await client.declare_topology()
    lane = _current_lane()
    queue = _lane_queue(CHAT_REQUEST.queue, lane)
    await client.consume(queue, handle_chat_request)
    logger.info("Chat request consumer started (queue=%s)", queue)
