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


CHAT_RESPONSE = Route("chat_response", "chat.response")
RECALL = Route("recall", "action.recall")


# ---------------------------------------------------------------------------
# Channel-partitioned outbound routes
# ---------------------------------------------------------------------------
# 出站队列的分区维度必须跟消费者的所有权维度一致。chat_response / recall 的 owner
# 按 channel 拆成两个服务之后，共用一条队列意味着 RabbitMQ 轮询把流量随机劈成两半
# —— 不报错、不留痕，切流永远做不干净。
#
# 命名口径与 TS 侧 packages/ts-shared/src/mq/client.ts::channelRoute 逐字一致：
# channel 揉进 base 名，泳道后缀继续加在最后。
#
#   chat_response  →  chat_response_lark  →  chat_response_lark_{lane}
#   chat.response  →  chat.response.lark  →  chat.response.lark.{lane}
#
# 这样 lane_queue / _lane_rk 原样套用，而且泳道队列的 x-dead-letter-routing-key
# 取的就是 route.rk —— TTL 到期自动弹回**同 channel** 的 prod rk。弹到别的 channel
# 上意味着泳道的回复由另一个渠道发出去，比不弹严重得多。
def channel_route(base: Route, channel: str) -> Route:
    """Return ``base`` partitioned by ``channel``."""
    return Route(
        queue=f"{base.queue}_{channel}",
        rk=f"{base.rk}.{channel}",
        lane_fallback=base.lane_fallback,
    )


# 已知 channel 是一份显式清单，不动态发现：动态发现意味着队列可能压根没被声明，
# 而声明缺失是静默的（topic exchange 上没有绑定的 rk，消息直接消失）。TS 侧各服务
# 维护自己那一份，跨语言没法共享 —— 加新渠道时两边都要改。
KNOWN_CHANNELS: tuple[str, ...] = ("lark", "qq")

_CHANNEL_PARTITIONED_BASES = (CHAT_RESPONSE, RECALL)

CHANNEL_PARTITIONED_ROUTES: dict[str, dict[str, Route]] = {
    base.queue: {c: channel_route(base, c) for c in KNOWN_CHANNELS}
    for base in _CHANNEL_PARTITIONED_BASES
}

CHANNEL_ROUTES: list[Route] = [
    route
    for by_channel in CHANNEL_PARTITIONED_ROUTES.values()
    for route in by_channel.values()
]


def channel_route_for(base_queue: str, channel: str) -> Route:
    """Resolve the channel-partitioned Route for ``base_queue``.

    fail-closed on both axes: publishing to an unregistered channel would
    hit a routing key nothing is bound to and the message would vanish
    without an error.
    """
    by_channel = CHANNEL_PARTITIONED_ROUTES.get(base_queue)
    if by_channel is None:
        raise ValueError(
            f"queue={base_queue!r} is not channel-partitioned "
            f"({sorted(CHANNEL_PARTITIONED_ROUTES)}); it has a single Route."
        )
    route = by_channel.get(channel)
    if route is None:
        raise ValueError(
            f"channel={channel!r} not in KNOWN_CHANNELS ({list(KNOWN_CHANNELS)}); "
            f"queue {base_queue}_{channel} is never declared, so publishing "
            f"there would silently drop the message. Register the channel in "
            f"app/infra/rabbitmq.py (and in the TS side's owned-channel list)."
        )
    return route


def channel_route_for_payload(base_queue: str, payload: Any) -> Route:
    """Resolve the partitioned Route for ``base_queue`` off the payload's channel.

    分区队列的 rk 只有消息自己知道，所以一律现算。出站（sink dispatch）和 DLQ 重放
    共用这一条规则 —— 重放要是照着 base 名发，消息会落在一条没有消费者的队列上静默
    滞留，而审计写着 published 成功。

    缺 channel / 未注册的 channel 一律抛，不挑默认值：猜错的下场是回复从另一个渠道
    发出去，比明确失败糟得多。
    """
    channel = payload.get("channel") if isinstance(payload, dict) else None
    if not isinstance(channel, str) or not channel:
        raise ValueError(
            f"queue={base_queue!r} is channel-partitioned but the payload carries "
            f"no channel ({channel!r}); refusing to guess a routing key."
        )
    return channel_route_for(base_queue, channel)


# runtime_delayed_trigger queues (Phase 7a Gap 9.1.2): one per origin
# APP_NAME so an envelope published from agent-service is consumed only
# by an agent-service runtime (preserving emit()'s in-process / cross-
# process fan-out decisions which depend on APP_NAME). Lane queues use
# lane_fallback=False so a feat-x lane envelope never spills into prod.
# （vectorize-worker 随 v4 记忆整机删除，已无任何节点，不再注册。）
KNOWN_APPS_FOR_DELAYED_TRIGGER = ["agent-service"]
DELAYED_TRIGGER_ROUTES = [
    Route(
        queue=f"runtime_delayed_trigger_{app}",
        rk=f"runtime.delayed_trigger.{app}",
        lane_fallback=False,
    )
    for app in KNOWN_APPS_FOR_DELAYED_TRIGGER
]


def trigger_route_for(app: str) -> Route:
    """Return the runtime_delayed_trigger Route for ``app``.

    Caller MUST pass an app from KNOWN_APPS_FOR_DELAYED_TRIGGER; an
    unknown app would publish to a queue that no consumer subscribes
    to and the envelope would never fire.
    """
    if app not in KNOWN_APPS_FOR_DELAYED_TRIGGER:
        raise ValueError(
            f"app={app!r} not in KNOWN_APPS_FOR_DELAYED_TRIGGER "
            f"({KNOWN_APPS_FOR_DELAYED_TRIGGER}); update the list in "
            f"app/infra/rabbitmq.py to register a new origin app."
        )
    for r in DELAYED_TRIGGER_ROUTES:
        if r.queue == f"runtime_delayed_trigger_{app}":
            return r
    raise RuntimeError(f"trigger route for {app!r} not registered")  # unreachable

# ALL_ROUTES 身兼两职，删东西之前先看清是哪一职：
#
#   声明面   declare_topology 遍历它建队列 + 绑定
#   注册面   Sink.mq(name) / Source.mq(name) 的合法队列名从它来
#            （compile_graph 的 known_queues、sink_dispatch._route_by_queue、
#            dlq_admin 的重放目标查表）
#
# base 的 chat_response / recall 在声明面上已经是死的：出站早就按 channel 分区，
# 生产者只发 chat.response.{channel} / action.recall.{channel}，消费者也只订
# {queue}_{channel}，两条 base 队列没有生产者也没有消费者。
#
# 但它们在注册面上是活的：wiring 里写的是 Sink.mq("chat_response") /
# Sink.mq("recall")，**base 名就是逻辑 sink 的标识**，真实 rk 由 _dispatch_mq_sink
# 按 payload 的 channel 现算（channel_route_for）。从 ALL_ROUTES 里摘掉它们，
# compile_graph 会在启动时直接 GraphError（"queue not in ALL_ROUTES"），进程起不来。
#
# 所以这两条留着，代价是两条空队列。要真正去掉得先把注册面从声明面里拆出来。
#
# chat_request 两职都已经没了，所以它整条摘掉：她不从队列拿消息 —— 每一缝直接查
# common_message、自己决定要不要开口（app/living），入站两个渠道服务也不再往它上面
# 投。留着的话就是一条没有生产者、没有消费者、没有 TTL 也没有长度上限的队列。
ALL_ROUTES = [
    CHAT_RESPONSE,
    RECALL,
    *CHANNEL_ROUTES,
    *DELAYED_TRIGGER_ROUTES,
]

# ---------------------------------------------------------------------------
# Lane helpers
# ---------------------------------------------------------------------------
MessageHandler = Callable[[AbstractIncomingMessage], Coroutine[Any, Any, None]]


def current_lane() -> str | None:
    """Return current lane (None means prod).

    contract-allowed None (§4.8): "no lane" is a normal value (prod),
    not a failure. The middleware-import try/except handles the worker
    path where contextvars aren't installed; ENV ``LANE`` is the
    fallback. No upstream call here.
    """
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
      自己 lane 上等到期；codex review round-1 M5 + round-5 H1)
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
                arguments=_build_queue_args(route.rk, lane, route.lane_fallback),
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

        Reads ``route.lane_fallback`` (default True for prod compatibility) to
        decide whether the lane queue gets x-message-ttl-back-to-prod fallback.
        debounce routes set ``lane_fallback=False`` so 300s delays don't get
        short-circuited to prod (spec §3.4.4 / codex review round-5 H1).
        """
        if self._channel is None or self._exchange is None:
            raise RuntimeError("must call connect() + declare_topology() first")
        lane = current_lane()
        q = await self._channel.declare_queue(
            lane_queue(route.queue, lane),
            durable=True,
            arguments=_build_queue_args(route.rk, lane, route.lane_fallback),
        )
        await q.bind(self._exchange, routing_key=_lane_rk(route.rk, lane))

    async def _ensure_lane_queue(self, route: Route, lane: str) -> None:
        """Lazily declare a lane queue on first publish (reads route.lane_fallback)."""
        cache_key = f"{route.queue}_{lane}"
        if cache_key in self._declared_lane_queues:
            return
        if self._channel is None:
            raise RuntimeError("must call connect() first")
        q = await self._channel.declare_queue(
            lane_queue(route.queue, lane),
            durable=True,
            arguments=_build_queue_args(route.rk, lane, route.lane_fallback),
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

    async def publish_with_confirm(
        self,
        route: Route,
        body: dict,
        *,
        delay_ms: int | None = None,
        headers: dict | None = None,
        lane: str | None = ...,  # type: ignore[assignment]
        timeout_s: float = 5.0,
    ) -> bool:
        """Publish with broker publish-confirm; return True iff broker ack-ed.

        Used by the durable retry transport (Gap 7.2) and emit_delayed
        durable path (Gap 9). Caller decides on False — DLQ-fallback for
        retry, or raise for emit_delayed.

        aio-pika channels default to ``publisher_confirms=True`` so the
        underlying ``exchange.publish`` already awaits broker confirm. We
        wrap with a hard timeout + broad exception catch so a transient
        network blip never raises out into the durable handler (which
        already owns ack/nack semantics via ``message.process``).
        """
        import asyncio

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
        try:
            await asyncio.wait_for(
                self._exchange.publish(message, routing_key=actual_rk),
                timeout=timeout_s,
            )
            return True
        except TimeoutError:
            logger.exception(
                "publish_with_confirm timed out: queue=%s rk=%s",
                route.queue,
                actual_rk,
            )
            return False
        except Exception:
            logger.exception(
                "publish_with_confirm failed: queue=%s rk=%s",
                route.queue,
                actual_rk,
            )
            return False

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


async def basic_get(queue: str, *, no_ack: bool = False):
    """Phase 7b Gap 12: blocking basic.get for DLQ replay.

    Returns aio_pika.IncomingMessage or None if queue is empty. Caller is
    responsible for ack()/nack(requeue=...) on the returned message.

    Uses the shared ``mq._channel`` — caller must ensure ``mq.connect()``
    has been called before invoking this helper (same precondition as all
    other ``mq`` operations).
    """
    if mq._channel is None:
        raise RuntimeError("must call mq.connect() first")
    queue_obj = await mq._channel.declare_queue(queue, passive=True)
    return await queue_obj.get(timeout=5, fail=False, no_ack=no_ack)
