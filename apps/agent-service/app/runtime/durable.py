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
import re
from collections.abc import Callable
from typing import Any

from aio_pika.abc import AbstractIncomingMessage

from app.api.middleware import lane_var, trace_id_var
from app.infra.rabbitmq import Route, mq
from app.runtime.data import Data
from app.runtime.node import inputs_of
from app.runtime.persist import insert_idempotent
from app.runtime.wire import WireSpec

logger = logging.getLogger(__name__)


_CAMEL_TO_SNAKE_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_TO_SNAKE_2 = re.compile(r"([a-z0-9])([A-Z])")


def _snake(name: str) -> str:
    """Convert ``CamelCase`` to ``camel_case``."""
    s = _CAMEL_TO_SNAKE_1.sub(r"\1_\2", name)
    return _CAMEL_TO_SNAKE_2.sub(r"\1_\2", s).lower()


def _route_for(w: WireSpec, consumer: Callable) -> Route:
    """Build the ``(queue, routing_key)`` for a durable wire + consumer."""
    data_snake = _snake(w.data_type.__name__)
    cname = consumer.__name__
    return Route(
        queue=f"durable_{data_snake}_{cname}",
        rk=f"durable.{data_snake}.{cname}",
    )


# Tracks (queue, consumer_tag) so ``stop_consumers`` can cancel each consumer
# deterministically before the connection is torn down.
_consumer_tags: list[tuple[Any, str]] = []


async def publish_durable(
    w: WireSpec, consumer: Callable, data: Data
) -> None:
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
            trace_id = headers.get("trace_id") or None
            lane = headers.get("lane") or None

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


async def start_consumers() -> None:
    """Declare and start consumers for every ``.durable()`` wire in the graph."""
    # Late import: compile_graph must see the final WIRING_REGISTRY, and
    # emit/durable live in a cycle-prone zone during startup.
    from app.runtime.graph import compile_graph

    graph = compile_graph()
    for w in graph.wires:
        if not w.durable:
            continue
        for consumer in w.consumers:
            route = _route_for(w, consumer)
            await mq.declare_route(route)
            handler = _build_handler(w, consumer)
            queue, tag = await mq.consume(route.queue, handler)
            _consumer_tags.append((queue, tag))
            logger.info(
                "durable consumer started: %s -> %s",
                route.queue,
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
