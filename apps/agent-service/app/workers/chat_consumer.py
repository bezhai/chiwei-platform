"""Chat request MQ consumer — route messages to personas and stream responses.

Consumes ``chat_request`` queue, routes to persona(s) via MessageRouter,
executes ``stream_chat`` for each, splits on ``---split---`` markers,
and publishes response segments to ``chat_response`` queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from uuid import uuid4

from aio_pika.abc import AbstractIncomingMessage

from app.api.middleware import (
    CHAT_FIRST_TOKEN,
    CHAT_PIPELINE_DURATION,
    CHAT_QUEUE_WAIT,
    CHAT_TOKENS,
    header_vars,
)
from app.chat.pipeline import stream_chat
from app.chat.router import MessageRouter
from app.data.queries import (
    is_chat_request_completed,
    resolve_bot_name_for_persona,
    set_agent_response_bot,
)
from app.data.session import get_session
from app.infra.rabbitmq import (
    CHAT_REQUEST,
    CHAT_RESPONSE,
    current_lane,
    lane_queue,
    mq,
)
from app.workers.common import mq_error_handler

logger = logging.getLogger(__name__)

SPLIT_MARKER = "---split---"
MAX_MESSAGES = 4


# ---------------------------------------------------------------------------
# MQ handler
# ---------------------------------------------------------------------------


@mq_error_handler()
async def handle_chat_request(message: AbstractIncomingMessage) -> None:
    """Consume chat_request queue, route to persona(s) and process."""
    async with message.process(requeue=False):
        body = json.loads(message.body)
        session_id = body.get("session_id")
        message_id = body.get("message_id")
        chat_id = body.get("chat_id")
        is_p2p = body.get("is_p2p", False)
        user_id = body.get("user_id")
        lane = body.get("lane")
        bot_name = body.get("bot_name")
        is_proactive = body.get("is_proactive", False)
        mentions = body.get("mentions", [])

        # MQ consumer bypasses HTTP middleware — inject contextvars manually
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

        async with get_session() as s:
            already_completed = await is_chat_request_completed(
                s,
                session_id,
                is_proactive=is_proactive,
            )
        if already_completed:
            logger.warning(
                "Skipping redelivered completed chat_request: session_id=%s, message_id=%s, is_proactive=%s",
                session_id,
                message_id,
                is_proactive,
            )
            return

        # Route: decide which persona(s) reply
        router = MessageRouter()
        persona_ids = await router.route(
            chat_id=chat_id or "",
            mentions=mentions,
            bot_name=bot_name or "",
            is_p2p=is_p2p,
            is_proactive=is_proactive,
        )

        if not persona_ids:
            logger.info("No persona to reply: message_id=%s", message_id)
            return

        base_payload = {
            "session_id": session_id,
            "message_id": message_id,
            "chat_id": chat_id,
            "is_p2p": is_p2p,
            "root_id": body.get("root_id"),
            "user_id": user_id,
            "lane": lane,
            "is_proactive": is_proactive,
            "bot_name": bot_name,
            "enqueued_at": body.get("enqueued_at"),
        }

        if len(persona_ids) == 1:
            await _process_for_persona(base_payload, persona_ids[0])
        else:
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
                        "Persona %s failed: %s\n%s",
                        persona_ids[i],
                        result,
                        "".join(
                            traceback.format_exception(
                                type(result), result, result.__traceback__
                            )
                        ),
                    )


# ---------------------------------------------------------------------------
# Per-persona streaming + split + publish
# ---------------------------------------------------------------------------


async def _process_for_persona(base_payload: dict, persona_id: str) -> None:
    """Execute stream_chat for a single persona and publish response segments."""
    t_start = time.monotonic()
    session_id = base_payload["session_id"]
    message_id = base_payload["message_id"]
    lane = base_payload.get("lane")
    is_proactive = base_payload.get("is_proactive", False)
    chat_id = base_payload.get("chat_id", "")

    # Resolve the bot_name for this persona in this chat
    async with get_session() as s:
        response_bot_name = await resolve_bot_name_for_persona(s, persona_id, chat_id)
    if not response_bot_name:
        response_bot_name = base_payload.get("bot_name", "")

    # Update agent_response DB row with resolved bot_name + persona_id
    try:
        async with get_session() as s:
            await set_agent_response_bot(s, session_id, response_bot_name, persona_id)
    except Exception as e:
        logger.warning("Failed to update agent_response: %s", e)

    base_response = {
        "session_id": session_id,
        "message_id": message_id,
        "chat_id": chat_id,
        "is_p2p": base_payload.get("is_p2p"),
        "root_id": base_payload.get("root_id"),
        "user_id": base_payload.get("user_id"),
        "lane": lane,
        "is_proactive": is_proactive,
        "bot_name": response_bot_name,
    }

    # MQ queue wait time
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

            # Detect split markers and publish segments
            pending = full_content[sent_length:]
            while SPLIT_MARKER in pending and messages_sent < MAX_MESSAGES - 1:
                idx = pending.index(SPLIT_MARKER)
                part = pending[:idx].strip()
                if part:
                    base_response["published_at"] = int(time.time() * 1000)
                    await mq.publish(
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

        # Monitor generator drain latency
        t_generator_exit = time.monotonic()
        generator_drain_ms = (t_generator_exit - t_last_token) * 1000
        if generator_drain_ms > 5000:
            logger.warning(
                "stream_chat generator drain slow: session_id=%s, drain_ms=%d",
                session_id,
                round(generator_drain_ms),
            )

        # Metrics
        t_stream_end = time.monotonic()
        if t_first_token is not None:
            CHAT_FIRST_TOKEN.observe(t_first_token - t_start)
        CHAT_PIPELINE_DURATION.labels(stage="agent_stream").observe(
            t_stream_end - t_start
        )
        CHAT_TOKENS.labels(type="text").inc(token_count)

        # Publish remaining content (strip residual split markers)
        remaining = full_content[sent_length:].replace(SPLIT_MARKER, "").strip()
        clean_full = full_content.replace(SPLIT_MARKER, "\n\n").strip()

        t_publish_start = time.monotonic()
        base_response["published_at"] = int(time.time() * 1000)

        final_content = (remaining or full_content) if (remaining or messages_sent == 0) else ""
        await mq.publish(
            CHAT_RESPONSE,
            {
                **base_response,
                "content": final_content,
                "full_content": clean_full,
                "status": "success",
                "part_index": messages_sent,
                "is_last": True,
            },
            lane=lane,
        )

        # Full pipeline timing
        t_end = time.monotonic()
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
                "stream_ms": round((t_stream_end - t_start) * 1000),
                "ttft_ms": round(
                    (t_first_token - t_start) * 1000
                    if t_first_token is not None
                    else 0.0
                ),
                "publish_ms": round((t_end - t_publish_start) * 1000),
                "total_ms": round((t_end - t_start) * 1000),
                "tokens": token_count,
                "parts": messages_sent + 1,
            },
        )

    except asyncio.CancelledError:
        logger.error(
            "Chat request CANCELLED: session_id=%s, persona=%s",
            session_id,
            persona_id,
        )
        raise
    except Exception as e:
        logger.error(
            "Chat request failed: session_id=%s, persona=%s, error=%s\n%s",
            session_id,
            persona_id,
            str(e),
            traceback.format_exc(),
        )
        await mq.publish(
            CHAT_RESPONSE,
            {
                **base_response,
                "content": "",
                "status": "failed",
                "error": str(e),
            },
            lane=lane,
        )


# ---------------------------------------------------------------------------
# Consumer startup
# ---------------------------------------------------------------------------


async def start_chat_consumer() -> None:
    """Connect MQ and start consuming the chat_request queue."""
    await mq.connect()
    await mq.declare_topology()
    lane = current_lane()
    queue = lane_queue(CHAT_REQUEST.queue, lane)
    await mq.consume(queue, handle_chat_request)
    logger.info("Chat request consumer started (queue=%s)", queue)
