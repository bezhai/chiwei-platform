"""RabbitMQ 客户端 — 单例，声明拓扑 + 发布/消费"""

import json
import logging
import os
from collections.abc import Callable, Coroutine
from typing import Any, NamedTuple

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import AbstractIncomingMessage

from app.config import settings

logger = logging.getLogger(__name__)

# 拓扑常量
EXCHANGE_NAME = "post_processing"
DLX_NAME = "post_processing_dlx"
DLQ_NAME = "dead_letters"


# Route 当前假设 queue:rk = 1:1，未来如果需要演进为更灵活的结构需另行讨论。
class Route(NamedTuple):
    queue: str
    rk: str


CHAT_REQUEST = Route("chat_request", "chat.request")
CHAT_RESPONSE = Route("chat_response", "chat.response")
SAFETY_CHECK = Route("safety_check", "post.safety.check")
RECALL = Route("recall", "action.recall")
VECTORIZE = Route("vectorize", "task.vectorize")

ALL_ROUTES = [CHAT_REQUEST, CHAT_RESPONSE, SAFETY_CHECK, RECALL, VECTORIZE]

# 非 prod 队列空闲自动删除（24h）
_NON_PROD_EXPIRES_MS = 86_400_000
_LANE_FALLBACK_TTL_MS = 10_000


def _current_lane() -> str | None:
    """获取当前泳道：优先 contextvars，fallback 到环境变量"""
    try:
        from app.utils.middlewares.trace import get_lane

        lane = get_lane()
    except Exception:
        lane = None
    if not lane:
        lane = os.getenv("LANE")
    if not lane or lane == "prod":
        return None
    return lane


def _lane_queue(base: str, lane: str | None) -> str:
    """返回泳道队列名：base 或 base_{lane}"""
    return f"{base}_{lane}" if lane else base


def _lane_rk(base: str, lane: str | None) -> str:
    """返回泳道 routing key：base 或 base.{lane}"""
    return f"{base}.{lane}" if lane else base

def _build_queue_args(prod_rk: str, lane: str | None) -> dict[str, Any]:
    """构建队列参数：prod 队列用 DLX → DLQ；lane 队列用 TTL → 主 exchange fallback 到 prod"""
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


MessageHandler = Callable[[AbstractIncomingMessage], Coroutine[Any, Any, None]]


class RabbitMQClient:
    """aio-pika 单例客户端"""

    _instance: "RabbitMQClient | None" = None

    def __init__(self) -> None:
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractRobustChannel | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None
        self._declared_lane_queues: set[str] = set()

    @classmethod
    def get_instance(cls) -> "RabbitMQClient":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def connect(self) -> None:
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
        """声明 exchange、queue、binding、DLX（按泳道隔离）"""
        assert self._channel is not None, "must call connect() first"

        lane = _current_lane()

        # DLX + DLQ
        dlx = await self._channel.declare_exchange(
            DLX_NAME, ExchangeType.FANOUT, durable=True
        )
        dlq = await self._channel.declare_queue(DLQ_NAME, durable=True)
        await dlq.bind(dlx)

        # 主 exchange (delayed-message)
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
            await q.bind(
                self._exchange, routing_key=_lane_rk(route.rk, lane)
            )

        logger.info("RabbitMQ topology declared (lane=%s)", lane or "prod")

    async def _ensure_lane_queue(self, route: Route, lane: str) -> None:
        """懒声明泳道队列：publish 时若目标泳道队列尚未声明，则自动创建并绑定"""
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
        """发布消息。lane 默认取 _current_lane()，传 None 强制 prod。"""
        assert self._exchange is not None, "must call declare_topology() first"

        if lane is ...:
            lane = _current_lane()
        # prod / 空 → None
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
        assert self._channel is not None, "must call connect() first"

        queue = await self._channel.get_queue(queue_name)
        await queue.consume(callback)
        logger.info("Consuming queue: %s", queue_name)

    async def close(self) -> None:
        if self._connection and not self._connection.is_closed:
            await self._connection.close()
            logger.info("RabbitMQ connection closed")
