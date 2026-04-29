"""Runtime support for ``wire(T).debounce(...)``.

Pipeline shape:

    upstream emit -> publish_debounce
        |
        SET latest = trigger_id (redis, atomic with INCR count) +
        mq.publish delayed body (carries trigger_id + data)
        |
    handler picks up the delayed message
        |
        atomic-claim: stale-check (latest == trigger_id?) +
                      clear count = 0 in one Lua script
        |
    consumer (e.g. drift_check) runs
        |
        either returns normally (handler conditional-DELs latest+count)
        or raises DebounceReschedule(SameTrigger)
        (handler runs _do_reschedule with its own trigger_id)
        or raises any other exception (DLQ)

Reschedule API is intentionally not exposed at module level; business
nodes signal a reschedule by raising DebounceReschedule, the handler
holds the trigger_id and runs the CAS swap. Anything else (calling a
reschedule function from a background task that inherited a contextvar,
say) is unrepresentable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from aio_pika.abc import AbstractIncomingMessage

from app.api.middleware import lane_var, trace_id_var
from app.infra.rabbitmq import Route, current_lane, lane_queue, mq
from app.infra.redis import get_redis
from app.runtime.data import Data
from app.runtime.naming import to_snake
from app.runtime.node import inputs_of
from app.runtime.placement import nodes_for_app
from app.runtime.wire import WireSpec

logger = logging.getLogger(__name__)

# 24h covers typical outage windows; redis state expires past that, but
# at-least-once delivery from mq + business pipelines that auto-recover
# on next event make the cliff acceptable (see spec §4.1).
_DEFAULT_TTL_SECONDS = 86400


# ---------------------------------------------------------------------------
# Lua scripts
# ---------------------------------------------------------------------------

# publish_debounce: atomic SET latest + INCR count, with max_buffer trip:
# when count crosses the threshold, atomically reset count to 0 and tell
# the caller to flag this publish as fire_now=1 (immediate-fire path).
_PUBLISH_LUA = """
local ttl = tonumber(ARGV[2])
local max_buffer = tonumber(ARGV[3])
redis.call('SET', KEYS[1], ARGV[1], 'EX', ttl)
local n = redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[2], ttl)
local fire_now = 0
if n >= max_buffer then
    redis.call('SET', KEYS[2], 0, 'EX', ttl)
    fire_now = 1
end
return {n, fire_now}
"""

# handler atomic claim: stale-check (latest == this trigger?) and clear
# count = 0 in one shot. Returns 1 = claimed, 0 = stale.
_CLAIM_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
redis.call('SET', KEYS[2], 0, 'EX', ARGV[2])
return 1
"""

# handler conditional DEL: only delete latest+count if latest is still
# this trigger_id. If a reschedule swap or a real new event has
# overwritten latest, leave it alone.
_CONDITIONAL_DEL_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('DEL', KEYS[1])
    redis.call('DEL', KEYS[2])
    return 1
end
return 0
"""

# reschedule CAS swap: only set latest = new trigger_id when latest is
# still trigger_id_orig (handler's). If a real new event has already
# taken over, no-op and let that timer fire.
_RESCHEDULE_CAS_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""


# ---------------------------------------------------------------------------
# Public exception sentinel (signals lock contention from a business node)
# ---------------------------------------------------------------------------


class DebounceReschedule(Exception):
    """Raised by a debounce consumer when it can't process this fire and
    wants the handler to schedule another one.

    Usage::

        if not await redis.set(lock_key, token, nx=True, ex=600):
            raise DebounceReschedule(SameTrigger(...))

    The handler catches this, runs ``_do_reschedule(...)`` with its own
    trigger_id, and skips the conditional DEL so the fresh latest survives.

    Why a sentinel exception and not a public ``reschedule()`` function:
    Python copies contextvars into ``asyncio.create_task()``-spawned tasks.
    A public reschedule that read the handler's trigger_id from a contextvar
    could be called from a background task that inherited the var and run
    well after the handler's lifecycle ended. Sentinel raise from inside
    the consumer call keeps trigger_id confined to the handler's local
    scope.
    """

    def __init__(self, data: Data) -> None:
        super().__init__(f"debounce reschedule: {type(data).__name__}")
        self.data = data


# Note: there is intentionally no module-level `reschedule(data)` function.
# CAS swap + publish lives inside the handler via _do_reschedule (Task 8).


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

_consumer_tags: list[tuple[Any, str]] = []


def _route_for(w: WireSpec, consumer: Callable) -> Route:
    """Build the (queue, routing_key, lane_fallback=False) Route for a
    debounce wire.

    debounce route ALWAYS sets lane_fallback=False — long delays
    (300s afterthought) cannot be short-circuited to prod by the lane
    queue's x-message-ttl=10000.
    """
    data_snake = to_snake(w.data_type.__name__)
    return Route(
        queue=f"debounce_{data_snake}_{consumer.__name__}",
        rk=f"debounce.{data_snake}.{consumer.__name__}",
        lane_fallback=False,
    )


# ---------------------------------------------------------------------------
# Upstream emit path
# ---------------------------------------------------------------------------


async def publish_debounce(w: WireSpec, consumer: Callable, data: Data) -> None:
    """Upstream emit path for a debounced wire.

    Atomically SETs latest = trigger_id (overwriting any older one) and
    INCRs count. If count crosses max_buffer, the Lua resets count to 0
    and flags this publish as fire_now=1 (delay=0 immediate fire).
    """
    key = w.debounce_key_by(data)
    seconds = w.debounce["seconds"]
    max_buffer = w.debounce["max_buffer"]
    trigger_id = uuid.uuid4().hex
    redis = await get_redis()
    redis_latest = f"debounce:latest:{w.data_type.__name__}:{key}"
    redis_count = f"debounce:count:{w.data_type.__name__}:{key}"
    ttl_seconds = max(seconds * 2, _DEFAULT_TTL_SECONDS)

    result = await redis.eval(
        _PUBLISH_LUA, 2,
        redis_latest, redis_count,
        trigger_id, ttl_seconds, max_buffer,
    )
    new_count, fire_now_flag = int(result[0]), int(result[1])

    body = {
        "trigger_id": trigger_id,
        "data": data.model_dump(mode="json"),
        "key": key,
        "fire_now": bool(fire_now_flag),
    }
    headers = {
        "trace_id": trace_id_var.get() or "",
        "lane": lane_var.get() or "",
        "data_type": type(data).__name__,
    }
    delay_ms = 0 if body["fire_now"] else seconds * 1000
    await mq.publish(_route_for(w, consumer), body, headers=headers, delay_ms=delay_ms)
    logger.debug(
        "debounce publish: %s key=%s count=%d fire_now=%s",
        w.data_type.__name__, key, new_count, body["fire_now"],
    )


async def _do_reschedule(
    w: WireSpec, consumer: Callable, data: Data, trigger_id_orig: str,
) -> None:
    """Handler-internal reschedule: CAS swap latest + publish delay.

    Called by ``_build_handler`` when the consumer raises
    ``DebounceReschedule``. ``trigger_id_orig`` is the handler's local
    trigger_id (the one whose atomic-claim succeeded), passed in as a
    plain parameter so it never escapes through a contextvar.

    The Lua CAS swap only writes the new trigger_id when latest is still
    trigger_id_orig (no real new event has taken over). On collision,
    this no-ops and lets the new event's timer drive the next fire.
    """
    key = w.debounce_key_by(data)
    seconds = w.debounce["seconds"]
    new_trigger_id = uuid.uuid4().hex
    redis = await get_redis()
    redis_latest = f"debounce:latest:{w.data_type.__name__}:{key}"
    ttl_seconds = max(seconds * 2, _DEFAULT_TTL_SECONDS)

    swapped = await redis.eval(
        _RESCHEDULE_CAS_LUA, 1,
        redis_latest, trigger_id_orig, new_trigger_id, ttl_seconds,
    )
    if not int(swapped):
        logger.debug(
            "_do_reschedule no-op: latest already replaced for %s key=%s",
            type(data).__name__, key,
        )
        return

    body = {
        "trigger_id": new_trigger_id,
        "data": data.model_dump(mode="json"),
        "key": key,
        "fire_now": False,
    }
    headers = {
        "trace_id": trace_id_var.get() or "",
        "lane": lane_var.get() or "",
        "data_type": type(data).__name__,
    }
    await mq.publish(_route_for(w, consumer), body, headers=headers,
                     delay_ms=seconds * 1000)
    logger.info(
        "debounce reschedule: %s key=%s new_trigger_id=%s",
        type(data).__name__, key, new_trigger_id,
    )


def _build_handler(w: WireSpec, consumer: Callable):
    """Build the aio-pika message handler for one ``(wire, consumer)`` pair.

    Flow per message:
      1. Restore trace_id / lane contextvars from headers.
      2. Decode body, extract trigger_id / data / key.
      3. Atomic claim Lua: stale check + clear count = 0.
         claim returns 0 → drop (stale, message ack-ed by message.process).
      4. Decode Data, call consumer:
         (a) returns normally → conditional DEL latest+count if still ours.
         (b) raises ``DebounceReschedule(new_data)`` → run ``_do_reschedule``
             with our trigger_id; skip conditional DEL (CAS already wrote
             the new latest, or CAS no-op'd because a real new event took
             over — either way we don't want to clobber).
         (c) raises any other exception → propagate out of
             ``message.process``, which nacks (requeue=False) to DLX.
             Skip conditional DEL so latest survives for a manual DLQ
             replay (count is gone — see §4.1; replay restores fire signal
             only, not count).

    DebounceReschedule MUST be caught inside the ``async with`` block so
    aio_pika ack-s the original message; bubbling it out would route the
    payload to the DLX. Other exceptions intentionally propagate so the
    DLQ keeps owning the failure surface.
    """
    data_cls = w.data_type
    param_name = next(iter(inputs_of(consumer)))

    async def handler(message: AbstractIncomingMessage) -> None:
        async with message.process(requeue=False):
            headers = message.headers or {}
            # Defensive coercion (mirrors durable.py): non-string / empty
            # header values are treated as "not set" rather than crashing
            # downstream trace helpers.
            raw_trace = headers.get("trace_id")
            trace_id = raw_trace if isinstance(raw_trace, str) and raw_trace else None
            raw_lane = headers.get("lane")
            lane = raw_lane if isinstance(raw_lane, str) and raw_lane else None
            t_tok = trace_id_var.set(trace_id)
            l_tok = lane_var.set(lane)
            try:
                payload = json.loads(message.body)
                trigger_id = payload["trigger_id"]
                data_dict = payload["data"]
                key = payload["key"]

                redis = await get_redis()
                redis_latest = f"debounce:latest:{data_cls.__name__}:{key}"
                redis_count = f"debounce:count:{data_cls.__name__}:{key}"
                ttl_seconds = max(w.debounce["seconds"] * 2, _DEFAULT_TTL_SECONDS)

                # Atomic claim: stale check + clear count = 0. Even
                # fire_now=True messages run the claim — backlog of older
                # fire_now-flagged delays must not double-fire after the
                # count was reset by a newer publish (round-1 H3).
                claimed = await redis.eval(
                    _CLAIM_LUA, 2,
                    redis_latest, redis_count,
                    trigger_id, ttl_seconds,
                )
                if not int(claimed):
                    logger.debug(
                        "debounce drop stale: %s key=%s trigger_id=%s",
                        data_cls.__name__, key, trigger_id,
                    )
                    return

                obj = data_cls(**data_dict)
                logger.info(
                    "debounce fire: %s key=%s trigger_id=%s",
                    data_cls.__name__, key, trigger_id,
                )

                try:
                    await consumer(**{param_name: obj})
                except DebounceReschedule as resched:
                    logger.info(
                        "debounce reschedule: %s key=%s old_trigger_id=%s",
                        data_cls.__name__, key, trigger_id,
                    )
                    # trigger_id passed in as a plain parameter so it never
                    # escapes through a contextvar (round-7 M1).
                    await _do_reschedule(w, consumer, resched.data, trigger_id)
                    return  # 不走 conditional DEL：CAS 已处理 latest

                # Consumer 正常 return → conditional DEL：仅当 latest 还
                # 指向自己 trigger_id 时才删（reschedule / 真新事件覆盖
                # 时 no-op，避免清掉别人的 fire 信号）。
                await redis.eval(
                    _CONDITIONAL_DEL_LUA, 2,
                    redis_latest, redis_count,
                    trigger_id,
                )
            finally:
                trace_id_var.reset(t_tok)
                lane_var.reset(l_tok)

    return handler


# ---------------------------------------------------------------------------
# Consumer lifecycle
# ---------------------------------------------------------------------------


async def start_debounce_consumers(app_name: str | None = None) -> None:
    """Declare and start consumers for every ``.debounce()`` wire.

    Filters by ``app_name`` via ``nodes_for_app`` (wires whose consumers
    aren't bound to this app are skipped). compile_graph layer-4 already
    rejects mixed-app wires, so the "all consumers in allowed" check is
    strict-by-design.

    Not re-entrant: a second call without an intervening
    :func:`stop_debounce_consumers` would register duplicate RabbitMQ
    consumers on the same queue (double-fire) and fail noisily at
    shutdown — raise instead so the caller bug surfaces immediately.
    """
    if _consumer_tags:
        raise RuntimeError(
            "debounce consumers already started; call stop_debounce_consumers() first"
        )

    # Late import: compile_graph must observe the final WIRING_REGISTRY.
    from app.runtime.graph import compile_graph

    graph = compile_graph()

    allowed: set | None = None
    if app_name is not None:
        allowed = nodes_for_app(app_name)

    # Only touch RabbitMQ if this app actually has debounce consumers to
    # start — apps/tests without debounce wires shouldn't be forced to
    # configure RABBITMQ_URL just to boot.
    has_debounce = any(
        w.debounce is not None
        and (allowed is None or all(c in allowed for c in w.consumers))
        for w in graph.wires
    )
    if has_debounce:
        await mq.connect()
        await mq.declare_topology()

    for w in graph.wires:
        if w.debounce is None:
            continue
        if allowed is not None and not all(c in allowed for c in w.consumers):
            # Wire belongs to a different app. compile_graph layer-4 has
            # already ruled out mixed-app wires, so this is a clean skip.
            continue
        for consumer in w.consumers:
            route = _route_for(w, consumer)
            await mq.declare_route(route)
            handler = _build_handler(w, consumer)
            # declare_route declares the *lane-scoped* queue; consume must
            # target that same name, otherwise non-prod lanes hit NOT_FOUND
            # when get_queue runs passive.
            actual_queue = lane_queue(route.queue, current_lane())
            queue, tag = await mq.consume(actual_queue, handler)
            _consumer_tags.append((queue, tag))
            logger.info(
                "debounce consumer started: %s -> %s",
                actual_queue, consumer.__name__,
            )


async def stop_debounce_consumers() -> None:
    """Cancel every debounce consumer started by :func:`start_debounce_consumers`.

    Cancelling via ``queue.cancel(tag)`` tells RabbitMQ to stop delivering
    to this channel and lets any in-flight handler finish its
    ``message.process()`` context. After this returns, the connection can
    be closed without racing late deliveries.
    """
    for queue, tag in _consumer_tags:
        try:
            await queue.cancel(tag)
        except Exception as e:  # pragma: no cover — best effort on teardown
            logger.warning("failed to cancel debounce consumer %s: %s", tag, e)
    _consumer_tags.clear()
    # Yield so any handler mid-``message.process()`` can complete.
    await asyncio.sleep(0)
