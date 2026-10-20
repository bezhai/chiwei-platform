"""Outbound queues: what an app hands to the services that deliver it (lark-outbound and
chat-response-worker consume ``chat_response_<channel>`` and ``recall_<channel>``, see
:data:`app.infra.rabbitmq.ALL_ROUTES`).

**Which type goes to which queue is registered with the plugin host** (``ctx.outbound(data_type,
queue)``): a plugin says it in its setup, the host takes it back when the plugin stops. Business
code hands the instance over with :func:`publish` and never names a queue. One type goes to one
queue.

**What goes on the wire.** The body is the instance's JSON. A queue partitioned by channel
(:data:`~app.infra.rabbitmq.CHANNEL_PARTITIONED_ROUTES`) takes the routing key the instance's own
``channel`` picks; a channel no route knows is refused rather than published into the void. The
lane is resolved once (:func:`~app.infra.propagation.outbound_context`: the bound context, then the
instance's own ``lane`` field, then the deployment's) and goes both into the headers and to the
queue, so the header never says prod while the message lands on a lane queue. The headers carry
``trace_id``, ``lane`` and ``data_type`` (the class name).

**Never inside an open transaction.** A publish is a send to the outside: once it returns it
cannot be rolled back, and a transaction that rolls back after it would leave a message out for a
state that was never stored. Callers publish after their commit (or before their own record of
having sent, as speaking and taking back do).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from app.infra.propagation import inject_context, outbound_context
from app.infra.rabbitmq import (
    ALL_ROUTES,
    CHANNEL_PARTITIONED_ROUTES,
    Route,
    channel_route_for_payload,
    mq,
)

# The queue each registered type is published to (only this process's registrations).
_routes: dict[type[BaseModel], Route] = {}


def register(data_type: type[BaseModel], queue: str) -> Callable[[], None]:
    """From now on ``data_type`` is published to ``queue``; returns what takes it back. The queue
    must be a declared route, a queue partitioned by channel needs a type with a ``channel`` field,
    and a type goes to one queue: anything else is a ``ValueError``, raised when the plugin
    registers it rather than at the first message."""
    route = next((r for r in ALL_ROUTES if r.queue == queue), None)
    if route is None:
        raise ValueError(
            f"outbound {data_type.__name__} -> {queue!r}: no route in ALL_ROUTES declares "
            f"that queue, so there is no routing key to publish it with"
        )
    if queue in CHANNEL_PARTITIONED_ROUTES and "channel" not in data_type.model_fields:
        raise ValueError(
            f"outbound {data_type.__name__} -> {queue!r}: the queue is partitioned by channel "
            f"and {data_type.__name__} has no 'channel' field to pick the routing key with"
        )
    if data_type in _routes:
        raise ValueError(
            f"outbound {data_type.__name__} is already published to "
            f"{_routes[data_type].queue!r}; a type goes to one queue"
        )
    _routes[data_type] = route

    def take_back() -> None:
        if _routes.get(data_type) is route:
            del _routes[data_type]

    return take_back


def clear() -> None:
    """Forget every registration: a process that starts afresh (and the tests that stand for one)
    begins with none."""
    _routes.clear()


def registered() -> dict[type[BaseModel], str]:
    """The types registered in this process and the queue each goes to."""
    return {data_type: route.queue for data_type, route in _routes.items()}


async def publish(data: BaseModel) -> None:
    """Publish ``data`` to the queue its type is registered to (see the module docstring). A type
    nobody registered is a ``LookupError``: a message the code meant to send is never dropped
    quietly."""
    try:
        route = _routes[type(data)]
    except KeyError:
        raise LookupError(
            f"outbound: nothing registered {type(data).__name__} to a queue "
            f"(ctx.outbound in the plugin that sends it)"
        ) from None
    body: dict[str, Any] = data.model_dump(mode="json")
    if route.queue in CHANNEL_PARTITIONED_ROUTES:
        route = channel_route_for_payload(route.queue, body)
    raw_lane = body.get("lane")
    ctx = outbound_context(
        fallback_lane=raw_lane if isinstance(raw_lane, str) and raw_lane else None
    )
    headers = inject_context({"data_type": type(data).__name__}, ctx)
    await mq.publish(route, body, headers=headers, lane=ctx.lane)
