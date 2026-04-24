"""Durable edges — RabbitMQ adapter for ``wire(T).to(c).durable()``.

Semantics:

  * ``publish_durable(w, c, data)`` always publishes to RabbitMQ. No
    publisher-side dedup (a pre-insert + skip-publish would desync the DB
    from the queue if RabbitMQ drops the message mid-flight).
  * The consumer side is where dedup lives: each handler calls
    ``insert_idempotent(obj)`` first. If it returns 0, the message is a
    duplicate of one already processed and the consumer is *not* invoked
    (handler acks and moves on). If it returns 1, the consumer runs.
    At-least-once delivery + a DB unique index gives effectively
    exactly-once effect.
  * Messages carry ``trace_id`` and ``lane`` in headers; the handler sets
    both contextvars for the duration of the consumer call.

Queue topology reuses the existing exchange from :mod:`app.infra.rabbitmq`
(``post_processing``, x-delayed-message, lane TTL fallback, DLX). Each
``(data_type, consumer)`` pair gets its own ``Route``:

  queue:        ``durable_<snake_data>_<consumer_name>``
  routing key:  ``durable.<snake_data>.<consumer_name>``
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from aio_pika.abc import AbstractIncomingMessage

from app.api.middleware import lane_var, trace_id_var
from app.infra.rabbitmq import Route, current_lane, lane_queue, mq
from app.runtime.data import Data
from app.runtime.naming import to_snake
from app.runtime.node import inputs_of
from app.runtime.persist import insert_idempotent
from app.runtime.wire import WireSpec

logger = logging.getLogger(__name__)


def _route_for(w: WireSpec, consumer: Callable) -> Route:
    """Build the ``(queue, routing_key)`` for a durable wire + consumer."""
    data_snake = to_snake(w.data_type.__name__)
    cname = consumer.__name__
    return Route(
        queue=f"durable_{data_snake}_{cname}",
        rk=f"durable.{data_snake}.{cname}",
    )


# Tracks (queue, consumer_tag) so ``stop_consumers`` can cancel each consumer
# deterministically before the connection is torn down.
_consumer_tags: list[tuple[Any, str]] = []


async def publish_durable(w: WireSpec, consumer: Callable, data: Data) -> None:
    """Publish ``data`` to the durable queue targeting ``consumer``.

    Always publishes — dedup is done on the consumer side via
    ``insert_idempotent``. Propagates current ``trace_id`` and ``lane`` via
    message headers so the consumer can restore them.
    """
    route = _route_for(w, consumer)
    headers: dict[str, Any] = {
        "trace_id": trace_id_var.get() or "",
        "lane": lane_var.get() or "",
        "data_type": type(data).__name__,
    }
    body = data.model_dump(mode="json")
    await mq.publish(route, body, headers=headers)


def _build_handler(w: WireSpec, consumer: Callable):
    """Build an aio-pika message handler for ``(wire, consumer)``.

    The handler:
      1. Restores ``trace_id`` / ``lane`` contextvars from message headers.
      2. Decodes the body into ``w.data_type(**payload)``.
      3. Calls ``insert_idempotent`` for dedup (returns 0 => duplicate,
         ack and no-op; returns 1 => proceed).
      4. Invokes ``consumer`` with the single input parameter bound to the
         decoded object (phase-0 MVP: durable consumers are single-input).
      5. ``message.process()`` ack-s on success / nacks-with-requeue on
         exception — aio-pika handles both; we only need to raise.
    """
    data_cls = w.data_type
    param_name = next(iter(inputs_of(consumer)))

    async def handler(message: AbstractIncomingMessage) -> None:
        # requeue=False: a bad payload (can't parse / duplicated) never
        # becomes a poison-loop; requeue=True would have aio-pika re-deliver
        # forever. Infra-level retries are the DLX's job, not ours.
        async with message.process(requeue=False):
            headers = message.headers or {}
            # Defensive coercion: contextvars are typed as Optional[str]
            # downstream, and a misbehaving publisher could send a list /
            # bytes / int under these header keys. Treat any non-string
            # (or empty string) value as "not set" rather than letting a
            # cryptic ``str()`` crash happen deep inside a trace helper.
            raw_trace = headers.get("trace_id")
            trace_id = raw_trace if isinstance(raw_trace, str) and raw_trace else None
            raw_lane = headers.get("lane")
            lane = raw_lane if isinstance(raw_lane, str) and raw_lane else None

            t_tok = trace_id_var.set(trace_id)
            l_tok = lane_var.set(lane)
            try:
                payload = json.loads(message.body)
                obj = data_cls(**payload)
                n = await insert_idempotent(obj)
                if n == 0:
                    logger.debug(
                        "durable consumer %s: duplicate %s, skipped",
                        consumer.__name__,
                        data_cls.__name__,
                    )
                    return
                await consumer(**{param_name: obj})
            finally:
                trace_id_var.reset(t_tok)
                lane_var.reset(l_tok)

    return handler


async def start_consumers(app_name: str | None = None) -> None:
    """Declare and start consumers for durable wires.

    Args:
        app_name: when ``None``, iterate every ``.durable()`` wire in the
            graph (legacy behavior — preserves the existing smoke test
            and durable tests). When set, filter to wires whose consumers
            are all bound to ``app_name`` via ``app.runtime.placement.bind``.
            Wires whose consumers span multiple apps are rejected at
            ``compile_graph`` time (layer-4 validation), so the "all
            consumers bound to this app" check here is strict-by-design.

    Not re-entrant: a second call without an intervening
    :func:`stop_consumers` would register duplicate RabbitMQ consumers on
    the same queue (double-processing) and then fail noisily at shutdown.
    We raise instead of silently returning so the caller bug surfaces
    immediately rather than masquerading as "it didn't take".
    """
    if _consumer_tags:
        raise RuntimeError("consumers already started; call stop_consumers() first")

    # Late import: compile_graph must see the final WIRING_REGISTRY, and
    # emit/durable live in a cycle-prone zone during startup.
    from app.runtime.graph import compile_graph

    graph = compile_graph()

    allowed: set | None = None
    if app_name is not None:
        from app.runtime.placement import nodes_for_app

        allowed = nodes_for_app(app_name)

    # Only touch RabbitMQ if this app actually has durable consumers to
    # start — otherwise tests / apps without durable wires would be
    # forced to configure RABBITMQ_URL just to boot.
    has_durable = any(
        w.durable
        and (allowed is None or all(c in allowed for c in w.consumers))
        for w in graph.wires
    )
    if has_durable:
        await mq.connect()
        await mq.declare_topology()

    for w in graph.wires:
        if not w.durable:
            continue
        if allowed is not None and not all(c in allowed for c in w.consumers):
            # Wire belongs to a different app. compile_graph layer-4 has
            # already ruled out mixed-app wires, so this is a clean skip.
            continue
        for consumer in w.consumers:
            route = _route_for(w, consumer)
            await mq.declare_route(route)
            handler = _build_handler(w, consumer)
            # declare_route declares the *lane-scoped* queue name; consume
            # must target that same name, otherwise non-prod lanes hit
            # NOT_FOUND when get_queue runs passive.
            actual_queue = lane_queue(route.queue, current_lane())
            queue, tag = await mq.consume(actual_queue, handler)
            _consumer_tags.append((queue, tag))
            logger.info(
                "durable consumer started: %s -> %s",
                actual_queue,
                consumer.__name__,
            )


async def stop_consumers() -> None:
    """Cancel every durable consumer started by :func:`start_consumers`.

    Cancelling via ``queue.cancel(tag)`` is the clean way — it tells
    RabbitMQ to stop delivering to this channel and lets any in-flight
    handler finish its ``message.process()`` context. After this returns,
    the connection can be closed without racing late deliveries.
    """
    for queue, tag in _consumer_tags:
        try:
            await queue.cancel(tag)
        except Exception as e:  # pragma: no cover — best effort on teardown
            logger.warning("failed to cancel consumer %s: %s", tag, e)
    _consumer_tags.clear()
    # Yield so any handler that was mid-``message.process()`` can complete.
    await asyncio.sleep(0)
