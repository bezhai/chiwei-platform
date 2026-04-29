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
    lane_fallback: bool = True   # debounce route 用 False；默认 True 不破坏现有 Route("queue", "rk") 调用


CHAT_REQUEST = Route("chat_request", "chat.request")
CHAT_RESPONSE = Route("chat_response", "chat.response")
RECALL = Route("recall", "action.recall")
# ``vectorize`` is published by lark-server (TS, identical
# ``buildQueueArgs``/``DLX_NAME``/``EXCHANGE_NAME`` constants) and
# consumed by the dataflow runtime via ``Source.mq("vectorize")``. We
# co-declare it from agent-service's ``ALL_ROUTES`` so a lane that
# only deploys agent-service + vectorize-worker (no lark-server in the
# lane) can still create the lane queue ``vectorize_<lane>`` —
# otherwise vectorize-worker's MQ source loop hits NOT_FOUND on
# passive ``get_queue`` and the runtime crashes. Re-declare on the
# prod-side queue is a no-op because both publishers compute identical
# queue args.
VECTORIZE = Route("vectorize", "task.vectorize")

# Memory v4 vectorize: split into per-row queues so each one maps 1:1
# onto a typed Data on the dataflow side (Source.mq today only decodes
# a single Data type per queue). Bodies:
#   memory_fragment_vectorize <- {"fragment_id": "f_xxx"}
#   memory_abstract_vectorize <- {"abstract_id": "a_xxx"}
MEMORY_FRAGMENT_VECTORIZE = Route(
    "memory_fragment_vectorize", "task.memory_fragment_vectorize"
)
MEMORY_ABSTRACT_VECTORIZE = Route(
    "memory_abstract_vectorize", "task.memory_abstract_vectorize"
)
ALL_ROUTES = [
    CHAT_REQUEST,
    CHAT_RESPONSE,
    RECALL,
    VECTORIZE,
    MEMORY_FRAGMENT_VECTORIZE,
    MEMORY_ABSTRACT_VECTORIZE,
]

# ---------------------------------------------------------------------------
# Lane helpers
# ---------------------------------------------------------------------------
MessageHandler = Callable[[AbstractIncomingMessage], Coroutine[Any, Any, None]]


def current_lane() -> str | None:
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


def lane_queue(base: str, lane: str | None) -> str:
    return f"{base}_{lane}" if lane else base


def _lane_rk(base: str, lane: str | None) -> str:
    return f"{base}.{lane}" if lane else base


def _build_queue_args(prod_rk: str, lane: str | None,
                     lane_fallback: bool = True) -> dict[str, Any]:
    """Build queue arguments.

    - prod queues: dead-letter to DLX
    - lane queues with lane_fallback=True: TTL -> main exchange with prod
      routing-key (fallback), plus auto-expire after 24 h idle
    - lane queues with lane_fallback=False: keep DLX (异常 nack 仍要进
      dead_letters), but no ttl-back-to-prod (long-delay messages 留在
      自己 lane 上等到期；reviewer round-1 M5 + round-5 H1)
    """
    extra: dict[str, Any] = {}
    if lane:
        extra["x-expires"] = _NON_PROD_EXPIRES_MS
    if not lane:
        return {"x-dead-letter-exchange": DLX_NAME, **extra}
    if not lane_fallback:
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
        """Declare exchange, queues, bindings, DLX (lane-isolated).

        Prod uses the ``x-delayed-message`` plugin on the main exchange so
        publishers can schedule delayed delivery. Test environments that
        run vanilla RabbitMQ images (no plugin) can set
        ``RABBITMQ_DISABLE_DELAYED=1`` to declare a plain topic exchange
        instead. Consumers don't care which mode is active — the delay is
        only used by producers that pass ``delay_ms=``.
        """
        if self._channel is None:
            raise RuntimeError("must call connect() first")

        lane = current_lane()

        # DLX + DLQ
        dlx = await self._channel.declare_exchange(
            DLX_NAME, ExchangeType.FANOUT, durable=True
        )
        dlq = await self._channel.declare_queue(DLQ_NAME, durable=True)
        await dlq.bind(dlx)

        # Main exchange (delayed-message plugin, topic in test envs)
        if os.getenv("RABBITMQ_DISABLE_DELAYED") == "1":
            self._exchange = await self._channel.declare_exchange(
                EXCHANGE_NAME,
                ExchangeType.TOPIC,
                durable=True,
            )
        else:
            self._exchange = await self._channel.declare_exchange(
                EXCHANGE_NAME,
                type="x-delayed-message",
                durable=True,
                arguments={"x-delayed-type": "topic"},
            )

        for route in ALL_ROUTES:
            q = await self._channel.declare_queue(
                lane_queue(route.queue, lane),
                durable=True,
                arguments=_build_queue_args(route.rk, lane),
            )
            await q.bind(self._exchange, routing_key=_lane_rk(route.rk, lane))

        logger.info("RabbitMQ topology declared (lane=%s)", lane or "prod")

    async def declare_route(self, route: Route) -> None:
        """Declare a single route's queue + binding on the main exchange.

        Used by the dataflow runtime to register durable wires dynamically on
        top of the existing lane-aware topology (DLX, lane-TTL fallback, lazy
        lane-queue declare all continue to work). ``declare_topology()`` still
        owns the static ``ALL_ROUTES`` list; this method is its per-route
        sibling so new routes can plug in without amending that list.
        """
        if self._channel is None or self._exchange is None:
            raise RuntimeError("must call connect() + declare_topology() first")
        lane = current_lane()
        q = await self._channel.declare_queue(
            lane_queue(route.queue, lane),
            durable=True,
            arguments=_build_queue_args(route.rk, lane),
        )
        await q.bind(self._exchange, routing_key=_lane_rk(route.rk, lane))

    async def _ensure_lane_queue(self, route: Route, lane: str) -> None:
        """Lazily declare a lane queue on first publish."""
        cache_key = f"{route.queue}_{lane}"
        if cache_key in self._declared_lane_queues:
            return
        if self._channel is None:
            raise RuntimeError("must call connect() first")
        q = await self._channel.declare_queue(
            lane_queue(route.queue, lane),
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
        if self._exchange is None:
            raise RuntimeError("must call declare_topology() first")

        if lane is ...:
            lane = current_lane()
        if lane == "prod":
            lane = None

        if lane:
            await self._ensure_lane_queue(route, lane)

        actual_rk = _lane_rk(route.rk, lane)

        msg_headers: dict[str, Any] = dict(headers) if headers else {}
        if delay_ms is not None:
            msg_headers["x-delay"] = delay_ms

        message = Message(
            body=json.dumps(body).encode(),
            delivery_mode=DeliveryMode.PERSISTENT,
            content_type="application/json",
            headers=msg_headers if msg_headers else None,
        )
        await self._exchange.publish(message, routing_key=actual_rk)

    async def consume(
        self, queue_name: str, callback: MessageHandler
    ) -> tuple[Any, str]:
        """Start consuming from a queue.

        Returns ``(queue, consumer_tag)`` so callers can cancel via
        ``queue.cancel(consumer_tag)``. Legacy callers may ignore the return.
        """
        if self._channel is None:
            raise RuntimeError("must call connect() first")
        queue = await self._channel.get_queue(queue_name)
        tag = await queue.consume(callback)
        logger.info("Consuming queue: %s", queue_name)
        return queue, tag

    async def close(self) -> None:
        """Close the connection."""
        if self._connection and not self._connection.is_closed:
            await self._connection.close()
            logger.info("RabbitMQ connection closed")


# Module-level instance
mq = _RabbitMQ()
