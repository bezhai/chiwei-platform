"""Tests for ``app.capabilities.redis`` (plan B5).

The ``RedisCapability`` wraps a raw ``redis.asyncio.Redis`` client behind a
domain-shaped API. Business code never reaches into the raw client; every
write/read goes through this capability so:

* Keys auto-prefix with ``{lane}:`` when ``current_lane()`` is non-None
  (prod stays bare-key so existing prod data isn't migrated).
* Raw redis failures map to the typed ``CapabilityCallFailed`` /
  ``CapabilityTimeout`` exceptions (contract §4.8).
* The acceptance scenario — two lanes concurrently running the same
  Lua against the same logical key — produces fully isolated state.

Uses ``fakeredis[lua]`` so Lua scripts execute against a real interpreter.
"""
from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest
import redis.exceptions

from app.api.middleware import lane_var
from app.capabilities._errors import (
    CapabilityCallFailed,
    CapabilityTimeout,
)
from app.capabilities.redis import RedisCapability


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def cap(fake_redis: fakeredis.aioredis.FakeRedis) -> RedisCapability:
    return RedisCapability(fake_redis)


@pytest.fixture
def lane_prod():
    """``current_lane()`` returns None — bare-key path."""
    token = lane_var.set(None)
    try:
        yield
    finally:
        lane_var.reset(token)


def _set_lane(name: str | None):
    """Helper: yield a context where ``current_lane()`` returns ``name``."""
    token = lane_var.set(name)
    return token


# ---------------------------------------------------------------------------
# Basic API surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incr_returns_int(cap, lane_prod):
    n = await cap.incr("counter:a")
    assert n == 1
    n2 = await cap.incr("counter:a")
    assert n2 == 2


@pytest.mark.asyncio
async def test_incr_with_amount(cap, lane_prod):
    n = await cap.incr("counter:b", amount=5)
    assert n == 5


@pytest.mark.asyncio
async def test_eval_runs_lua(cap, lane_prod, fake_redis):
    """Lua script gets to manipulate redis state; KEYS / ARGV plumbed."""
    script = """
    redis.call('SET', KEYS[1], ARGV[1])
    return redis.call('GET', KEYS[1])
    """
    result = await cap.eval(script, keys=["k:eval"], args=["hello"])
    assert result == "hello"
    # state visible on the raw client too (under the same key, since prod = no prefix)
    assert await fake_redis.get("k:eval") == "hello"


# ---------------------------------------------------------------------------
# Lane key space: no implicit prefix
# ---------------------------------------------------------------------------
#
# Contract (post-hotfix 2026-05-13): RedisCapability does NOT inject any
# lane prefix into keys. Cross-lane isolation is the ConfigBundle's job —
# coe-* lanes get a physically separate Redis container (chiwei-test) via
# ``class_overrides[coe]``; ppe-* lanes intentionally share prod Redis
# because that's the whole point of ppe ("functional verification against
# real prod data"). An implicit ``{lane}:`` prefix broke that contract: it
# made ppe-* agent-service write to ``ppe-foo:<key>`` while the prod
# reader of the same logical key read it bare and silently missed every
# entry — values dropped on lane verification (see trace
# 3de371aea10290b327f1386ea56f180c).


@pytest.mark.asyncio
async def test_prod_lane_no_prefix(cap, lane_prod, fake_redis):
    """Prod lane stores keys bare — baseline."""
    await cap.incr("c:prod")
    assert await fake_redis.get("c:prod") == "1"


@pytest.mark.asyncio
async def test_non_prod_lane_also_no_prefix(cap, fake_redis):
    """Non-prod lanes share the same key space — capability does not
    silently rewrite keys based on lane."""
    token = _set_lane("ppe-foo")
    try:
        await cap.incr("c:lane")
    finally:
        lane_var.reset(token)
    # Same bare key as prod would have written.
    assert await fake_redis.get("c:lane") == "1"
    assert await fake_redis.get("ppe-foo:c:lane") is None


@pytest.mark.asyncio
async def test_eval_keys_pass_through_unchanged(cap, fake_redis):
    """``eval`` passes ``keys`` to Lua verbatim regardless of lane."""
    token = _set_lane("coe-bar")
    script = """
    redis.call('SET', KEYS[1], ARGV[1])
    redis.call('SET', KEYS[2], ARGV[2])
    return 1
    """
    try:
        await cap.eval(script, keys=["k:a", "k:b"], args=["v1", "v2"])
    finally:
        lane_var.reset(token)
    assert await fake_redis.get("k:a") == "v1"
    assert await fake_redis.get("k:b") == "v2"
    # No phantom prefixed keys.
    assert await fake_redis.get("coe-bar:k:a") is None
    assert await fake_redis.get("coe-bar:k:b") is None


# ---------------------------------------------------------------------------
# Acceptance: two lanes concurrently running the same Lua, independent state
# ---------------------------------------------------------------------------


# A read-modify-write Lua with a per-key counter — the shape that makes
# cross-lane key-space bleed visible: if two lanes shared a key space the
# counters would interleave instead of each starting at 1.
_COUNT_AND_STORE_LUA = """
local key = KEYS[1]
local value = ARGV[1]
local ttl = tonumber(ARGV[2])

local n = redis.call('HINCRBY', key, '__counter__', 1)
redis.call('HSET', key, 'entry:' .. n, value)
redis.call('EXPIRE', key, ttl)
return n
"""


async def _count_and_store(cap: RedisCapability, slot: str, value: str) -> int:
    """Run the counter Lua for one (lane, slot) pair."""
    key = f"cap:counted:{slot}"
    n = await cap.eval(_COUNT_AND_STORE_LUA, keys=[key], args=[value, 1800])
    return int(n)


@pytest.mark.asyncio
async def test_coe_lane_isolation_via_separate_redis_instances(fake_redis):
    """Acceptance (post-hotfix 2026-05-13): cross-lane Redis isolation
    is the ConfigBundle's job, not the capability's.

    coe-* lanes get a physically separate Redis container (chiwei-test)
    via ``class_overrides[coe]`` — different ``RedisCapability`` instances
    point at different clients. Two coe-* lanes running the same logical
    key against their own clients are naturally isolated; the capability
    never touches the key.

    ppe-* lanes share prod Redis on purpose ("functional verification
    against prod data") so cross-ppe isolation is intentionally NOT
    provided — that's a property of ppe, not a bug. The test below
    only exercises the coe case, which is the one we actually rely on.
    """
    import fakeredis.aioredis as fakeredis_aio

    # Two physically distinct fakeredis backing stores — simulating two
    # coe lanes pointing at two chiwei-test Redis containers via
    # ConfigBundle ``class_overrides[coe]``.
    coe_one_client = fakeredis_aio.FakeRedis(decode_responses=True)
    coe_two_client = fakeredis_aio.FakeRedis(decode_responses=True)
    cap_one = RedisCapability(coe_one_client)
    cap_two = RedisCapability(coe_two_client)

    async def run(cap: RedisCapability, value_prefix: str) -> list[int]:
        results = []
        for i in range(5):
            n = await _count_and_store(cap, slot="slot-1", value=f"{value_prefix}/{i}")
            results.append(n)
            await asyncio.sleep(0)
        return results

    res_one, res_two = await asyncio.gather(
        run(cap_one, "one"),
        run(cap_two, "two"),
    )

    assert res_one == [1, 2, 3, 4, 5]
    assert res_two == [1, 2, 3, 4, 5]

    one_hash = await coe_one_client.hgetall("cap:counted:slot-1")
    two_hash = await coe_two_client.hgetall("cap:counted:slot-1")
    assert one_hash["entry:1"] == "one/0"
    assert two_hash["entry:1"] == "two/0"
    # The "main" cap (prod-style shared fakeredis) is unaffected — it
    # would only contain entries written through itself.
    assert await fake_redis.hgetall("cap:counted:slot-1") == {}


# ---------------------------------------------------------------------------
# Typed-error mapping
# ---------------------------------------------------------------------------


class _RaisingRedis:
    """Stand-in for a redis client that fails every call with a chosen exc."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def incr(self, key, amount=1):  # noqa: ARG002
        raise self._exc

    async def eval(self, *a, **kw):  # noqa: ARG002
        raise self._exc

    async def smembers(self, key):  # noqa: ARG002
        raise self._exc

    async def get(self, key):  # noqa: ARG002
        raise self._exc

    async def set(self, *a, **kw):  # noqa: ARG002
        raise self._exc

    async def expire(self, *a, **kw):  # noqa: ARG002
        raise self._exc


@pytest.mark.asyncio
async def test_redis_error_maps_to_call_failed(lane_prod):
    cap = RedisCapability(_RaisingRedis(redis.exceptions.RedisError("boom")))
    with pytest.raises(CapabilityCallFailed) as ei:
        await cap.incr("k")
    assert "boom" in str(ei.value)
    assert ei.value.meta.get("op") == "incr"


@pytest.mark.asyncio
async def test_connection_error_maps_to_call_failed(lane_prod):
    cap = RedisCapability(_RaisingRedis(redis.exceptions.ConnectionError("conn")))
    with pytest.raises(CapabilityCallFailed):
        await cap.eval("return 1", keys=[], args=[])


@pytest.mark.asyncio
async def test_redis_timeout_maps_to_capability_timeout(lane_prod):
    cap = RedisCapability(_RaisingRedis(redis.exceptions.TimeoutError("slow")))
    with pytest.raises(CapabilityTimeout) as ei:
        await cap.incr("k")
    assert ei.value.meta.get("op") == "incr"


@pytest.mark.asyncio
async def test_asyncio_timeout_maps_to_capability_timeout(lane_prod):
    cap = RedisCapability(_RaisingRedis(asyncio.TimeoutError()))
    with pytest.raises(CapabilityTimeout):
        await cap.eval("return 1", keys=["k"], args=[])


# ---------------------------------------------------------------------------
# Set read accessor (added for C5 — banned_words)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smembers_returns_set(cap, lane_prod, fake_redis):
    await fake_redis.sadd("s:k", "a", "b", "c")
    assert await cap.smembers("s:k") == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_smembers_missing_returns_empty(cap, lane_prod):
    assert await cap.smembers("s:missing") == set()


@pytest.mark.asyncio
async def test_smembers_passes_key_through_unchanged(cap, fake_redis):
    token = _set_lane("ppe-z")
    try:
        await fake_redis.sadd("s:lane", "x", "y")
        assert await cap.smembers("s:lane") == {"x", "y"}
        assert await fake_redis.smembers("ppe-z:s:lane") == set()
    finally:
        lane_var.reset(token)


# Typed-error mapping for new accessors


@pytest.mark.asyncio
async def test_smembers_redis_error_maps_to_call_failed(lane_prod):
    cap = RedisCapability(_RaisingRedis(redis.exceptions.RedisError("boom")))
    with pytest.raises(CapabilityCallFailed) as ei:
        await cap.smembers("k")
    assert ei.value.meta.get("op") == "smembers"


@pytest.mark.asyncio
async def test_smembers_timeout_maps_to_capability_timeout(lane_prod):
    cap = RedisCapability(_RaisingRedis(redis.exceptions.TimeoutError("slow")))
    with pytest.raises(CapabilityTimeout):
        await cap.smembers("k")


# ---------------------------------------------------------------------------
# String get / set_with_ttl / expire (added for agent session续接)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_with_ttl_then_get(cap, lane_prod, fake_redis):
    await cap.set_with_ttl("s:k", "payload", ttl_seconds=60)
    assert await cap.get("s:k") == "payload"
    # TTL was applied
    assert 0 < await fake_redis.ttl("s:k") <= 60


@pytest.mark.asyncio
async def test_get_missing_returns_none(cap, lane_prod):
    assert await cap.get("s:missing") is None


@pytest.mark.asyncio
async def test_set_with_ttl_refreshes_ttl(cap, lane_prod, fake_redis):
    await cap.set_with_ttl("s:k", "v1", ttl_seconds=10)
    # rewriting with a longer ttl refreshes it (SET resets the key's expiry)
    await cap.set_with_ttl("s:k", "v2", ttl_seconds=100)
    assert await cap.get("s:k") == "v2"
    assert await fake_redis.ttl("s:k") > 10


@pytest.mark.asyncio
async def test_expire_refreshes_ttl(cap, lane_prod, fake_redis):
    await cap.set_with_ttl("s:k", "v", ttl_seconds=10)
    await cap.expire("s:k", 200)
    assert await fake_redis.ttl("s:k") > 10


@pytest.mark.asyncio
async def test_get_passes_key_through_unchanged(cap, fake_redis):
    token = _set_lane("ppe-s")
    try:
        await cap.set_with_ttl("s:lane", "v", ttl_seconds=60)
        assert await cap.get("s:lane") == "v"
        assert await fake_redis.get("ppe-s:s:lane") is None
    finally:
        lane_var.reset(token)


@pytest.mark.asyncio
async def test_get_redis_error_maps_to_call_failed(lane_prod):
    cap = RedisCapability(_RaisingRedis(redis.exceptions.RedisError("boom")))
    with pytest.raises(CapabilityCallFailed) as ei:
        await cap.get("k")
    assert ei.value.meta.get("op") == "get"


@pytest.mark.asyncio
async def test_set_with_ttl_timeout_maps_to_capability_timeout(lane_prod):
    cap = RedisCapability(_RaisingRedis(redis.exceptions.TimeoutError("slow")))
    with pytest.raises(CapabilityTimeout):
        await cap.set_with_ttl("k", "v", ttl_seconds=10)


@pytest.mark.asyncio
async def test_expire_redis_error_maps_to_call_failed(lane_prod):
    cap = RedisCapability(_RaisingRedis(redis.exceptions.RedisError("boom")))
    with pytest.raises(CapabilityCallFailed) as ei:
        await cap.expire("k", 10)
    assert ei.value.meta.get("op") == "expire"
