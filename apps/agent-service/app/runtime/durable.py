"""Durable edges — RabbitMQ adapter for ``wire(T).to(c).durable()``.

Delivery semantics (intentionally limited):

  * ``publish_durable(w, c, data)`` always publishes to RabbitMQ. No
    publisher-side dedup (a pre-insert + skip-publish would desync the
    DB from the queue if RabbitMQ drops the message mid-flight).
  * Consumer-side dedup: each handler calls ``insert_idempotent(obj)``
    first. If it returns 0 the message is a duplicate of one already
    processed and the consumer is *not* invoked (handler acks and moves
    on). If it returns 1, the consumer runs.
  * **Failure handling is fail-to-DLQ, not in-place retry.** A handler
    exception triggers ``message.process(requeue=False)`` — aio-pika
    nacks without requeue, the broker routes the message to the
    configured dead-letter exchange / queue, and that's where it stops.
    There is no automatic delay-retry chain; replaying a failed message
    is an operator action against the DLQ. Combined with insert_idempotent,
    delivery is **at-least-once via dedup** (a manual replay of an
    already-processed message is a no-op on the consumer side).
  * Messages carry ``trace_id`` and ``lane`` in headers; the handler
    sets both contextvars for the duration of the consumer call.

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
import os
import socket
from collections.abc import Callable
from typing import Any

from aio_pika.abc import AbstractIncomingMessage

from app.api.middleware import lane_var, trace_id_var
from app.infra.rabbitmq import Route, current_lane, lane_queue, mq
from app.runtime.data import Data
from app.runtime.errors import DuplicateData, NeedsReview
from app.runtime.inflight import (
    claim_inflight,
    edge_id_for,
    mark_failed,
    mark_history_backfill,
    mark_succeeded,
)
from app.runtime.migrator import _table_name
from app.runtime.naming import to_snake
from app.runtime.node import inputs_of
from app.runtime.persist import _dedup_hash, insert_idempotent
from app.runtime.propagation import (
    Context,
    bind_context,
    extract_context,
    inject_context,
)
from app.runtime.retry import DELIVERY_COUNT_HEADER, decide_retry
from app.runtime.wire import WireSpec

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
_DEFAULT_LEASE_MS = 300_000  # 5 min — only used when wire has no .retry()

logger = logging.getLogger(__name__)

# Module-level alias so tests can patch "app.runtime.durable.publish_with_confirm"
# without needing to reach into mq.publish_with_confirm.
publish_with_confirm = mq.publish_with_confirm


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
    body = data.model_dump(mode="json")
    # data.lane fallback is publish_durable-specific (Data instance carries its
    # own lane field for some pipelines); not part of the generic propagation
    # primitive. Compose Context manually to honor the fallback.
    data_lane = body.get("lane")
    effective_lane = lane_var.get() or (
        data_lane if isinstance(data_lane, str) and data_lane else None
    )
    headers = inject_context(
        {"data_type": type(data).__name__},
        Context(trace_id=trace_id_var.get(), lane=effective_lane),
    )
    await mq.publish(route, body, headers=headers, lane=effective_lane or None)


def _idempotent_key_for(obj: Data) -> str:
    """Compute the inflight idempotent_key for ``obj``.

    Uses ``Meta.dedup_column`` value when declared (adoption mode),
    otherwise the runtime-managed ``dedup_hash``. Mirrors
    ``insert_idempotent``'s conflict-target rule.
    """
    cls = type(obj)
    meta = getattr(cls, "Meta", None)
    dedup_col = getattr(meta, "dedup_column", None) if meta else None
    if dedup_col:
        return str(getattr(obj, dedup_col))
    return _dedup_hash(obj)


def _build_handler(w: WireSpec, consumer: Callable):
    """Build an aio-pika message handler for ``(wire, consumer)``.

    The handler:
      1. Restore trace_id / lane contextvars from message headers.
      2. Decode body into ``w.data_type(**payload)``.
      3. Claim inflight state via the runtime_inflight state machine
         (Gap 7.1):
         - row missing → INSERT processing(attempts=1, lease) — fresh=True
         - succeeded → ack + return  (dedup terminal)
         - processing-with-live-lease → ack + return  (peer worker)
         - processing-expired / failed → take over, attempts++ — fresh=False
      4. On fresh claim (non-adoption Data only), call insert_idempotent:
         - n == 0 → Data row pre-existed (pre-7a or concurrent writer).
           Mark inflight succeeded + ack without invoking the consumer
           (history compatibility, Gap 7.1.1).
         - n == 1 → Data row newly written; continue to consumer.
         Adoption-mode Data skips this step (its consumer-side dedup
         carries the original guarantee).
      5. Invoke consumer; mark succeeded on success, mark failed on
         exception. Retry transport (republish with x-delay) is
         introduced in Task 5; this commit retains the legacy
         fail-to-DLQ via ``message.process(requeue=False)``.
    """
    data_cls = w.data_type
    param_name = next(iter(inputs_of(consumer)))
    edge_id = edge_id_for(data_cls.__qualname__, consumer.__qualname__)
    lease_ms = w.retry.lease_ms if w.retry is not None else _DEFAULT_LEASE_MS
    data_table = _table_name(data_cls)
    meta = getattr(data_cls, "Meta", None)
    is_adoption = meta is not None and getattr(meta, "existing_table", None) is not None

    async def handler(message: AbstractIncomingMessage) -> None:
        # requeue=False: a bad payload (can't parse / duplicated) never
        # becomes a poison-loop; requeue=True would have aio-pika re-deliver
        # forever. Infra-level retries are the DLX's job, not ours.
        async with message.process(requeue=False):
            ctx = extract_context(message.headers)
            async with bind_context(ctx):
                payload = json.loads(message.body)
                obj = data_cls(**payload)
                idem_key = _idempotent_key_for(obj)

                outcome = await claim_inflight(
                    edge_id=edge_id,
                    idempotent_key=idem_key,
                    data_table=data_table,
                    worker_id=WORKER_ID,
                    lease_ms=lease_ms,
                    trace_id=ctx.trace_id,
                )
                if outcome.action == "skip":
                    logger.debug(
                        "durable consumer %s: dedup-skip %s key=%s",
                        consumer.__name__,
                        data_cls.__name__,
                        idem_key,
                    )
                    return

                # Fresh claim → write the Data row (event persistence) +
                # history backfill detection. Adoption-mode Data skips
                # this step (its existing_table is owned by another
                # writer; consumer-side dedup carries idempotency).
                if outcome.fresh and not is_adoption:
                    n = await insert_idempotent(obj)
                    if n == 0:
                        # Data row pre-existed → history backfill. Mark
                        # inflight succeeded and ack without invoking the
                        # consumer.
                        logger.info(
                            "durable consumer %s: history-backfill %s key=%s",
                            consumer.__name__,
                            data_cls.__name__,
                            idem_key,
                        )
                        await mark_history_backfill(
                            edge_id=edge_id,
                            idempotent_key=idem_key,
                            data_table=data_table,
                        )
                        return

                logger.info(
                    "durable consumer %s: processing %s key=%s attempts=%d",
                    consumer.__name__,
                    data_cls.__name__,
                    idem_key,
                    outcome.attempts,
                )
                try:
                    await consumer(**{param_name: obj})
                except Exception as exc:
                    await _route_consumer_exception(
                        exc, wire=w, consumer=consumer,
                        inflight_key=(edge_id, idem_key),
                        data=obj, attempts=outcome.attempts,
                        headers=dict(message.headers or {}),
                    )
                else:
                    await mark_succeeded(
                        edge_id=edge_id,
                        idempotent_key=idem_key,
                    )

    return handler


async def _route_consumer_exception(
    exc: BaseException,
    *,
    wire: WireSpec,
    consumer: Callable,
    inflight_key: tuple[str, str],
    data: Data,
    attempts: int,
    headers: dict | None = None,
) -> None:
    """Phase 7b Gap 18: dispatch a consumer exception per wire.on_error.

    Contract:
      - return -> 'handled' path: caller's ``async with message.process(...)``
        will ack on clean exit. inflight terminal state is updated here.
      - raise  -> 'dlq' path: caller's ``process(requeue=False)`` will nack
        and the broker will route to DLX. Always re-raises the ORIGINAL
        exception (so DLQ message body keeps the cause).

    Helper itself NEVER calls message.ack() / message.nack() — see project
    memory feedback_aio_pika_process_context_double_ack.
    """
    edge_id, idem_key = inflight_key
    last_error = str(exc)

    # 1. typed exception in matching policy
    if isinstance(exc, DuplicateData) and wire.on_error == "ignore-duplicate":
        logger.warning(
            "durable consumer: duplicate ignored (edge=%s key=%s reason=%s)",
            edge_id, idem_key, last_error,
        )
        await mark_succeeded(edge_id=edge_id, idempotent_key=idem_key)
        return

    if isinstance(exc, NeedsReview) and wire.on_error == "manual-review":
        confirmed = await publish_to_review_queue(
            wire=wire, data=data, exc=exc,
            attempts=attempts, last_error=last_error,
        )
        if not confirmed:
            logger.warning(
                "durable consumer: review queue publish-confirm failed, "
                "falling through to DLQ (edge=%s key=%s)",
                edge_id, idem_key,
            )
            await mark_failed(edge_id=edge_id, idempotent_key=idem_key,
                              last_error=last_error)
            raise exc
        await mark_review(edge_id=edge_id, idempotent_key=idem_key)
        return

    # 2. generic Exception path (incl. typed exceptions in mismatched policies)
    await mark_failed(edge_id=edge_id, idempotent_key=idem_key,
                      last_error=last_error)
    decision = decide_retry(
        headers=headers or {},
        policy=wire.retry,
    )
    if decision.action == "retry":
        new_headers = dict(headers or {})
        new_headers[DELIVERY_COUNT_HEADER] = decision.attempt
        body = data.model_dump(mode="json")
        retry_lane = new_headers.get("lane") or None
        if isinstance(retry_lane, str) and not retry_lane:
            retry_lane = None
        route = _route_for(wire, consumer)
        confirmed = await publish_with_confirm(
            route, body, headers=new_headers,
            lane=retry_lane, delay_ms=decision.delay_ms,
        )
        if not confirmed:
            logger.warning(
                "durable consumer: retry publish-confirm failed, falling "
                "through to DLQ (edge=%s key=%s attempt=%d)",
                edge_id, idem_key, decision.attempt,
            )
            raise exc
        logger.info(
            "durable consumer: retry queued attempt=%d delay_ms=%d key=%s",
            decision.attempt, decision.delay_ms, idem_key,
        )
        return

    # 3. dlq fallback (.on_error("manual-review") with retry-exhausted handled here too)
    if wire.on_error == "manual-review":
        confirmed = await publish_to_review_queue(
            wire=wire, data=data, exc=exc,
            attempts=attempts, last_error=last_error,
        )
        if not confirmed:
            logger.warning(
                "durable consumer: review queue publish-confirm failed, "
                "falling through to DLQ (edge=%s key=%s)",
                edge_id, idem_key,
            )
            raise exc
        await mark_review(edge_id=edge_id, idempotent_key=idem_key)
        return

    raise exc  # default on_error="dlq" -> caller's process(requeue=False)


# Phase 7b temporary stubs — replaced in Task 3 when manual-review queue lands.
async def publish_to_review_queue(*args, **kwargs):  # pragma: no cover
    raise NotImplementedError("manual-review queue not yet wired (Task 3)")


async def mark_review(*args, **kwargs):  # pragma: no cover
    raise NotImplementedError("manual-review marking not yet wired (Task 3)")


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
        w.durable and (allowed is None or all(c in allowed for c in w.consumers))
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
