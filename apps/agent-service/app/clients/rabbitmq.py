"""RabbitMQ 客户端 — 单例，声明拓扑 + 发布/消费"""

import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import AbstractIncomingMessage

from app.config import settings

logger = logging.getLogger(__name__)

# 拓扑常量
EXCHANGE_NAME = "post_processing"
DLX_NAME = "post_processing_dlx"
DLQ_NAME = "dead_letters"

QUEUE_SAFETY_CHECK = "safety_check"
QUEUE_RECALL = "recall"

RK_SAFETY_CHECK = "post.safety.check"
RK_RECALL = "action.recall"

MessageHandler = Callable[[AbstractIncomingMessage], Coroutine[Any, Any, None]]


class RabbitMQClient:
    """aio-pika 单例客户端"""

    _instance: "RabbitMQClient | None" = None

    def __init__(self) -> None:
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractRobustChannel | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None

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
        logger.info("RabbitMQ connected: %s", url.split("@")[-1])

    async def declare_topology(self) -> None:
        """声明 exchange、queue、binding、DLX"""
        assert self._channel is not None, "must call connect() first"

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

        # safety_check queue
        q_safety = await self._channel.declare_queue(
            QUEUE_SAFETY_CHECK,
            durable=True,
            arguments={"x-dead-letter-exchange": DLX_NAME},
        )
        await q_safety.bind(self._exchange, routing_key=RK_SAFETY_CHECK)

        # recall queue
        q_recall = await self._channel.declare_queue(
            QUEUE_RECALL,
            durable=True,
            arguments={"x-dead-letter-exchange": DLX_NAME},
        )
        await q_recall.bind(self._exchange, routing_key=RK_RECALL)

        logger.info("RabbitMQ topology declared")

    async def publish(
        self,
        routing_key: str,
        body: dict,
        delay_ms: int | None = None,
        headers: dict | None = None,
    ) -> None:
        assert self._exchange is not None, "must call declare_topology() first"

        msg_headers: dict[str, Any] = headers or {}
        if delay_ms is not None:
            msg_headers["x-delay"] = delay_ms

        message = Message(
            body=json.dumps(body).encode(),
            delivery_mode=DeliveryMode.PERSISTENT,
            content_type="application/json",
            headers=msg_headers if msg_headers else None,
        )
        await self._exchange.publish(message, routing_key=routing_key)

    async def consume(self, queue_name: str, callback: MessageHandler) -> None:
        assert self._channel is not None, "must call connect() first"

        queue = await self._channel.get_queue(queue_name)
        await queue.consume(callback)
        logger.info("Consuming queue: %s", queue_name)

    async def close(self) -> None:
        if self._connection and not self._connection.is_closed:
            await self._connection.close()
            logger.info("RabbitMQ connection closed")
