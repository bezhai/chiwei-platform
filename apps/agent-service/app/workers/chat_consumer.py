"""Chat request MQ consumer

消费 chat_request queue，路由到对应 persona，
流式执行 agent 聊天，检测 ---split--- 分隔符实时切分发送到 chat_response queue。
"""

import asyncio
import json
import logging
import time
import traceback
from uuid import uuid4

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
    """消费 chat_request queue 中的消息，路由到对应 persona 并行处理"""
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
        is_proactive = body.get("is_proactive", False)
        mentions = body.get("mentions", [])

        # MQ consumer 不走 HTTP 中间件，手动注入 contextvars
        if bot_name:
            header_vars["app_name"].set(bot_name)
        if lane:
            header_vars["lane"].set(lane)

        logger.info(
            "Chat request received: session_id=%s, message_id=%s, lane=%s, bot_name=%s, mentions=%s",
            session_id,
            message_id,
            lane,
            bot_name,
            mentions,
        )

        # 路由：决定哪些 persona 回复
        from app.services.message_router import MessageRouter

        router = MessageRouter()
        persona_ids = await router.route(
            chat_id=chat_id or "",
            mentions=mentions,
            bot_name=bot_name or "",
            is_p2p=is_p2p,
        )

        if not persona_ids:
            logger.info("No persona to reply: message_id=%s", message_id)
            return

        # 构建公共 payload
        base_payload = {
            "session_id": session_id,
            "message_id": message_id,
            "chat_id": chat_id,
            "is_p2p": is_p2p,
            "root_id": root_id,
            "user_id": user_id,
            "lane": lane,
            "is_proactive": is_proactive,
            "bot_name": bot_name,
            "enqueued_at": body.get("enqueued_at"),
        }

        if len(persona_ids) == 1:
            # 单 persona：复用原始 session_id（向后兼容）
            await _process_for_persona(base_payload, persona_ids[0])
        else:
            # 多 persona：并行处理，第一个复用原始 session_id，后续生成新的
            tasks = []
            for i, pid in enumerate(persona_ids):
                payload = {**base_payload}
                if i > 0:
                    payload["session_id"] = str(uuid4())
                tasks.append(_process_for_persona(payload, pid))
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(
                        "Persona %s failed in gather: %s\n%s",
                        persona_ids[i], result, "".join(traceback.format_exception(type(result), result, result.__traceback__)),
                    )


async def _process_for_persona(base_payload: dict, persona_id: str) -> None:
    """为单个 persona 执行完整的 stream_chat + 发布 response 流程"""
    t_start = time.monotonic()
    session_id = base_payload["session_id"]
    message_id = base_payload["message_id"]
    lane = base_payload.get("lane")
    is_proactive = base_payload.get("is_proactive", False)

    # 从 persona_id 反查 bot_name（用于 chat.response，确保 worker 端用正确的 Lark 凭据）
    from app.services.bot_context import _resolve_bot_name_for_persona

    chat_id = base_payload.get("chat_id", "")
    response_bot_name = await _resolve_bot_name_for_persona(persona_id, chat_id)

    # 写入 persona_id + 修正 bot_name（makeTextReply 创建时用的是 SETNX 锁赢家的 bot_name）
    try:
        from app.orm.base import AsyncSessionLocal
        from sqlalchemy import text as sql_text

        async with AsyncSessionLocal() as session:
            await session.execute(
                sql_text(
                    "UPDATE agent_responses SET bot_name = :bn, persona_id = :pid "
                    "WHERE session_id = :sid"
                ),
                {"bn": response_bot_name, "pid": persona_id, "sid": session_id},
            )
            await session.commit()
    except Exception as e:
        logger.warning("Failed to update agent_response: %s", e)

    client = RabbitMQClient.get_instance()

    base_response = {
        "session_id": session_id,
        "message_id": message_id,
        "chat_id": base_payload.get("chat_id"),
        "is_p2p": base_payload.get("is_p2p"),
        "root_id": base_payload.get("root_id"),
        "user_id": base_payload.get("user_id"),
        "lane": lane,
        "is_proactive": is_proactive,
        "bot_name": response_bot_name,
    }

    # Measure MQ queue wait time
    queue_wait_ms = 0.0
    enqueued_at = base_payload.get("enqueued_at")
    if enqueued_at:
        queue_wait_s = (time.time() * 1000 - enqueued_at) / 1000
        queue_wait_ms = queue_wait_s * 1000
        CHAT_QUEUE_WAIT.observe(queue_wait_s)

    try:
        sent_length = 0
        messages_sent = 0
        full_content = ""
        t_first_token: float | None = None
        token_count = 0

        t_last_token = t_start
        async for text in stream_chat(
            message_id, session_id=session_id, persona_id=persona_id
        ):
            if not text:
                continue
            if t_first_token is None:
                t_first_token = time.monotonic()
            t_last_token = time.monotonic()
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
                        "Chat response part %d published: session_id=%s, persona=%s",
                        messages_sent - 1,
                        session_id,
                        persona_id,
                    )
                sent_length += idx + len(SPLIT_MARKER)
                pending = full_content[sent_length:]

        # 观测 last_token → stream_end 之间的延迟（正常应 <100ms，卡住时会很大）
        t_generator_exit = time.monotonic()
        generator_drain_ms = (t_generator_exit - t_last_token) * 1000
        if generator_drain_ms > 5000:
            logger.warning(
                "stream_chat generator drain slow: session_id=%s, drain_ms=%d, tokens=%d",
                session_id, round(generator_drain_ms), token_count,
            )

        # 记录流结束时间，观测 TTFT 和 agent_stream 阶段耗时
        t_stream_end = time.monotonic()
        stream_ms = (t_stream_end - t_start) * 1000
        logger.info(
            "Stream ended: session_id=%s, persona=%s, full_content_len=%d, token_count=%d, sent_length=%d, messages_sent=%d",
            session_id, persona_id, len(full_content), token_count, sent_length, messages_sent,
        )
        if t_first_token is not None:
            CHAT_FIRST_TOKEN.observe(t_first_token - t_start)
        CHAT_PIPELINE_DURATION.labels(stage="agent_stream").observe(
            t_stream_end - t_start
        )
        CHAT_TOKENS.labels(type="text").inc(token_count)

        # 流结束，发送剩余内容（去掉残留的 split marker）
        remaining = full_content[sent_length:].replace(SPLIT_MARKER, "").strip()
        # 全量文本（去掉所有 split marker），用于数据库存储
        clean_full = full_content.replace(SPLIT_MARKER, "\n\n").strip()

        logger.info(
            "Publishing final: session_id=%s, persona=%s, remaining_len=%d, clean_full_len=%d",
            session_id, persona_id, len(remaining), len(clean_full),
        )
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
            "Chat response final part %d published: session_id=%s, persona=%s",
            messages_sent,
            session_id,
            persona_id,
        )

        # 记录 MQ publish 耗时和全链路总时长
        t_end = time.monotonic()
        publish_ms = (t_end - t_publish_start) * 1000
        total_ms = (t_end - t_start) * 1000
        ttft_ms = (
            (t_first_token - t_start) * 1000 if t_first_token is not None else 0.0
        )
        CHAT_PIPELINE_DURATION.labels(stage="mq_publish").observe(
            t_end - t_publish_start
        )
        CHAT_PIPELINE_DURATION.labels(stage="total").observe(t_end - t_start)
        logger.info(
            "chat_request_done",
            extra={
                "event": "chat_request_done",
                "session_id": session_id,
                "persona_id": persona_id,
                "queue_wait_ms": round(queue_wait_ms),
                "stream_ms": round(stream_ms),
                "ttft_ms": round(ttft_ms),
                "publish_ms": round(publish_ms),
                "total_ms": round(total_ms),
                "tokens": token_count,
                "parts": messages_sent + 1,
            },
        )

    except asyncio.CancelledError:
        logger.error(
            "Chat request CANCELLED: session_id=%s, persona=%s, full_content_len=%d",
            session_id, persona_id, len(full_content),
        )
        raise  # CancelledError 应该继续传播
    except Exception as e:
        logger.error(
            "Chat request failed: session_id=%s, persona=%s, error=%s\n%s",
            session_id,
            persona_id,
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
