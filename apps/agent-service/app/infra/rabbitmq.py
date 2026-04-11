"""RabbitMQ client — module-level ``mq`` instance.

Declares topology with lane isolation + DLX dead-letter design.
Lane queues use TTL-based fallback to the prod routing key.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Coroutine
from typing import Any, NamedTuple

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import AbstractIncomingMessage

from app.infra.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Topology constants
# ---------------------------------------------------------------------------
EXCHANGE_NAME = "post_processing"
DLX_NAME = "post_processing_dlx"
DLQ_NAME = "dead_letters"

# Non-prod queues auto-expire after 24 h of inactivity
_NON_PROD_EXPIRES_MS = 86_400_000
# Lane queue TTL: messages fall back to prod after 10 s
_LANE_FALLBACK_TTL_MS = 10_000


# ---------------------------------------------------------------------------
# Routes — one queue + routing-key pair per logical stage
# ---------------------------------------------------------------------------
class Route(NamedTuple):
    queue: str
    rk: str


CHAT_REQUEST = Route("chat_request", "chat.request")
CHAT_RESPONSE = Route("chat_response", "chat.response")
SAFETY_CHECK = Route("safety_check", "post.safety.check")
RECALL = Route("recall", "action.recall")
VECTORIZE = Route("vectorize", "task.vectorize")
PROACTIVE_EVAL = Route("proactive_eval", "proactive.eval")

ALL_ROUTES = [
    CHAT_REQUEST,
    CHAT_RESPONSE,
    SAFETY_CHECK,
    RECALL,
    VECTORIZE,
    PROACTIVE_EVAL,
]

# ---------------------------------------------------------------------------
# Lane helpers
# ---------------------------------------------------------------------------
MessageHandler = Callable[[AbstractIncomingMessage], Coroutine[Any, Any, None]]


def _current_lane() -> str | None:
    """Return current lane (None means prod)."""
    try:
        from app.api.middleware import get_lane

        lane = get_lane()
    except Exception:
        lane = None
    if not lane:
        lane = os.getenv("LANE")
    if not lane or lane == "prod":
        return None
    return lane


def _lane_queue(base: str, lane: str | None) -> str:
    return f"{base}_{lane}" if lane else base


def _lane_rk(base: str, lane: str | None) -> str:
    return f"{base}.{lane}" if lane else base


def _build_queue_args(prod_rk: str, lane: str | None) -> dict[str, Any]:
    """Build queue arguments.

    - prod queues: dead-letter to DLX
    - lane queues: TTL -> main exchange with prod routing-key (fallback),
      plus auto-expire after 24 h idle
    """
    extra: dict[str, Any] = {}
    if lane:
        extra["x-expires"] = _NON_PROD_EXPIRES_MS
    if not lane:
        return {"x-dead-letter-exchange": DLX_NAME, **extra}
    return {
        "x-message-ttl": _LANE_FALLBACK_TTL_MS,
        "x-dead-letter-exchange": EXCHANGE_NAME,
        "x-dead-letter-routing-key": prod_rk,
        **extra,
    }


# ---------------------------------------------------------------------------
# RabbitMQ client
# ---------------------------------------------------------------------------
class _RabbitMQ:
    """aio-pika based RabbitMQ client with lane-aware topology."""

    def __init__(self) -> None:
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractRobustChannel | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None
        self._declared_lane_queues: set[str] = set()

    async def connect(self) -> None:
        """Connect (or reconnect) to RabbitMQ."""
        if self._connection and not self._connection.is_closed:
            return
        url = settings.rabbitmq_url
        if not url:
            raise RuntimeError("RABBITMQ_URL is not configured")
        self._connection = await aio_pika.connect_robust(url)
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=10)
        self._declared_lane_queues = set()
        logger.info("RabbitMQ connected: %s", url.split("@")[-1])

    async def declare_topology(self) -> None:
        """Declare exchange, queues, bindings, DLX (lane-isolated)."""
        assert self._channel is not None, "must call connect() first"

        lane = _current_lane()

        # DLX + DLQ
        dlx = await self._channel.declare_exchange(
            DLX_NAME, ExchangeType.FANOUT, durable=True
        )
        dlq = await self._channel.declare_queue(DLQ_NAME, durable=True)
        await dlq.bind(dlx)

        # Main exchange (delayed-message plugin)
        self._exchange = await self._channel.declare_exchange(
            EXCHANGE_NAME,
            type="x-delayed-message",
            durable=True,
            arguments={"x-delayed-type": "topic"},
        )

        for route in ALL_ROUTES:
            q = await self._channel.declare_queue(
                _lane_queue(route.queue, lane),
                durable=True,
                arguments=_build_queue_args(route.rk, lane),
            )
            await q.bind(self._exchange, routing_key=_lane_rk(route.rk, lane))

        logger.info("RabbitMQ topology declared (lane=%s)", lane or "prod")

    async def _ensure_lane_queue(self, route: Route, lane: str) -> None:
        """Lazily declare a lane queue on first publish."""
        cache_key = f"{route.queue}_{lane}"
        if cache_key in self._declared_lane_queues:
            return
        assert self._channel is not None, "must call connect() first"
        q = await self._channel.declare_queue(
            _lane_queue(route.queue, lane),
            durable=True,
            arguments=_build_queue_args(route.rk, lane),
        )
        await q.bind(self._exchange, routing_key=_lane_rk(route.rk, lane))
        self._declared_lane_queues.add(cache_key)
        logger.info("Lazy-declared lane queue: %s_%s", route.queue, lane)

    async def publish(
        self,
        route: Route,
        body: dict,
        delay_ms: int | None = None,
        headers: dict | None = None,
        lane: str | None = ...,  # type: ignore[assignment]
    ) -> None:
        """Publish a message. *lane* defaults to current lane; pass None for prod."""
        assert self._exchange is not None, "must call declare_topology() first"

        if lane is ...:
            lane = _current_lane()
        if lane == "prod":
            lane = None

        if lane:
            await self._ensure_lane_queue(route, lane)

        actual_rk = _lane_rk(route.rk, lane)

        msg_headers: dict[str, Any] = headers or {}
        if delay_ms is not None:
            msg_headers["x-delay"] = delay_ms

        message = Message(
            body=json.dumps(body).encode(),
            delivery_mode=DeliveryMode.PERSISTENT,
            content_type="application/json",
            headers=msg_headers if msg_headers else None,
        )
        await self._exchange.publish(message, routing_key=actual_rk)

    async def consume(self, queue_name: str, callback: MessageHandler) -> None:
        """Start consuming from a queue."""
        assert self._channel is not None, "must call connect() first"
        queue = await self._channel.get_queue(queue_name)
        await queue.consume(callback)
        logger.info("Consuming queue: %s", queue_name)

    async def close(self) -> None:
        """Close the connection."""
        if self._connection and not self._connection.is_closed:
            await self._connection.close()
            logger.info("RabbitMQ connection closed")


# Module-level instance
mq = _RabbitMQ()
