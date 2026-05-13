"""Tests for ``app.capabilities.redis`` (plan B5).

The ``RedisCapability`` wraps a raw ``redis.asyncio.Redis`` client behind a
domain-shaped API. Business code never reaches into the raw client; every
write/read goes through this capability so:

* Keys auto-prefix with ``{lane}:`` when ``current_lane()`` is non-None
  (prod stays bare-key so existing prod data isn't migrated).
* Raw redis failures map to the typed ``CapabilityCallFailed`` /
  ``CapabilityTimeout`` exceptions (contract §4.8).
* The acceptance scenario — two lanes concurrently running the same
  dedup Lua against the same logical key — produces fully isolated state.

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


@pytest.mark.asyncio
async def test_pipeline_batches(cap, lane_prod):
    async with cap.pipeline() as pipe:
        pipe.incr("counter:pipe")
        pipe.incr("counter:pipe")
        pipe.incr("counter:pipe")
        results = await pipe.execute()
    assert results == [1, 2, 3]


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
# made ppe-* agent-service write to ``ppe-foo:image_registry:...`` while
# prod chat-response-worker (which reads bare ``image_registry:...``)
# silently missed every entry — image links dropped on lane verification
# (see trace 3de371aea10290b327f1386ea56f180c).


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


@pytest.mark.asyncio
async def test_pipeline_keys_pass_through_unchanged(cap, fake_redis):
    token = _set_lane("ppe-pipe")
    try:
        async with cap.pipeline() as pipe:
            pipe.incr("c:p")
            pipe.incr("c:p")
            await pipe.execute()
    finally:
        lane_var.reset(token)
    assert await fake_redis.get("c:p") == "2"
    assert await fake_redis.get("ppe-pipe:c:p") is None


# ---------------------------------------------------------------------------
# Acceptance: two lanes concurrently running dedup Lua, independent state
# ---------------------------------------------------------------------------


# This is the actual ``infra/image.py`` register Lua, kept verbatim so the
# B5 acceptance scenario tests the real script shape.
_REGISTER_LUA = """
local key = KEYS[1]
local url = ARGV[1]
local ttl = tonumber(ARGV[2])

local n = redis.call('HINCRBY', key, '__counter__', 1)
local filename = n .. '.png'
redis.call('HSET', key, filename, url)
redis.call('EXPIRE', key, ttl)
return n
"""


async def _register(cap: RedisCapability, message_id: str, url: str) -> int:
    """Call the register Lua for one (lane, message) pair."""
    key = f"image_registry:{message_id}"
    n = await cap.eval(_REGISTER_LUA, keys=[key], args=[url, 1800])
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

    async def run(cap: RedisCapability, url_prefix: str) -> list[int]:
        results = []
        for i in range(5):
            n = await _register(cap, message_id="msg-1", url=f"{url_prefix}/{i}")
            results.append(n)
            await asyncio.sleep(0)
        return results

    res_one, res_two = await asyncio.gather(
        run(cap_one, "https://tos/one"),
        run(cap_two, "https://tos/two"),
    )

    assert res_one == [1, 2, 3, 4, 5]
    assert res_two == [1, 2, 3, 4, 5]

    one_hash = await coe_one_client.hgetall("image_registry:msg-1")
    two_hash = await coe_two_client.hgetall("image_registry:msg-1")
    assert one_hash["1.png"] == "https://tos/one/0"
    assert two_hash["1.png"] == "https://tos/two/0"
    # The "main" cap (prod-style shared fakeredis) is unaffected — it
    # would only contain entries written through itself.
    assert await fake_redis.hgetall("image_registry:msg-1") == {}


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

    async def hget(self, key, field):  # noqa: ARG002
        raise self._exc

    async def hgetall(self, key):  # noqa: ARG002
        raise self._exc

    async def smembers(self, key):  # noqa: ARG002
        raise self._exc

    def pipeline(self, *_a, **_kw):  # pragma: no cover — not used by these tests
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
# Hash + Set read accessors (added for C5 — image_registry / banned_words)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hget_returns_field(cap, lane_prod, fake_redis):
    await fake_redis.hset("h:k", "field1", "value1")
    assert await cap.hget("h:k", "field1") == "value1"


@pytest.mark.asyncio
async def test_hget_missing_returns_none(cap, lane_prod):
    assert await cap.hget("h:missing", "field1") is None


@pytest.mark.asyncio
async def test_hget_passes_key_through_unchanged(cap, fake_redis):
    """Capability does NOT rewrite the key on lane changes."""
    token = _set_lane("ppe-x")
    try:
        await fake_redis.hset("h:lane", "f", "v")
        assert await cap.hget("h:lane", "f") == "v"
        # The phantom prefixed key is empty.
        assert await fake_redis.hget("ppe-x:h:lane", "f") is None
    finally:
        lane_var.reset(token)


@pytest.mark.asyncio
async def test_hgetall_returns_dict(cap, lane_prod, fake_redis):
    await fake_redis.hset("h:all", mapping={"a": "1", "b": "2"})
    assert await cap.hgetall("h:all") == {"a": "1", "b": "2"}


@pytest.mark.asyncio
async def test_hgetall_missing_returns_empty(cap, lane_prod):
    assert await cap.hgetall("h:missing") == {}


@pytest.mark.asyncio
async def test_hgetall_passes_key_through_unchanged(cap, fake_redis):
    token = _set_lane("coe-y")
    try:
        await fake_redis.hset("h:all", mapping={"a": "1"})
        assert await cap.hgetall("h:all") == {"a": "1"}
        assert await fake_redis.hgetall("coe-y:h:all") == {}
    finally:
        lane_var.reset(token)


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
async def test_hget_redis_error_maps_to_call_failed(lane_prod):
    cap = RedisCapability(_RaisingRedis(redis.exceptions.RedisError("boom")))
    with pytest.raises(CapabilityCallFailed) as ei:
        await cap.hget("k", "f")
    assert ei.value.meta.get("op") == "hget"


@pytest.mark.asyncio
async def test_hget_timeout_maps_to_capability_timeout(lane_prod):
    cap = RedisCapability(_RaisingRedis(redis.exceptions.TimeoutError("slow")))
    with pytest.raises(CapabilityTimeout):
        await cap.hget("k", "f")


@pytest.mark.asyncio
async def test_hgetall_redis_error_maps_to_call_failed(lane_prod):
    cap = RedisCapability(_RaisingRedis(redis.exceptions.RedisError("boom")))
    with pytest.raises(CapabilityCallFailed) as ei:
        await cap.hgetall("k")
    assert ei.value.meta.get("op") == "hgetall"


@pytest.mark.asyncio
async def test_hgetall_timeout_maps_to_capability_timeout(lane_prod):
    cap = RedisCapability(_RaisingRedis(asyncio.TimeoutError()))
    with pytest.raises(CapabilityTimeout):
        await cap.hgetall("k")


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
