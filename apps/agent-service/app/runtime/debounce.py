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

import logging
from collections.abc import Callable
from typing import Any

from app.infra.rabbitmq import Route
from app.runtime.data import Data
from app.runtime.naming import to_snake
from app.runtime.wire import WireSpec

# Tasks 7-10 will add: asyncio, json, uuid, AbstractIncomingMessage,
# lane_var, trace_id_var, current_lane, lane_queue, mq, get_redis,
# inputs_of, nodes_for_app. Re-add when each is referenced (the imports
# are listed up-front in the plan as the full set this module ends up
# needing — kept here as intent doc only via this comment).

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
