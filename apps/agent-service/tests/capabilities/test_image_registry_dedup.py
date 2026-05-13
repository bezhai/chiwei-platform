"""C5 acceptance: ``ImageRegistry`` lane isolation via ``RedisCapability``.

The drill (plan C5 acceptance scenario): two lanes concurrently register
images against the same logical ``message_id`` — counters and storage stay
fully independent because the underlying ``RedisCapability`` auto-prefixes
keys with ``{lane}:``.

Backs ``app.infra.image.ImageRegistry`` against fakeredis + lane_var to
prove the cutover from raw ``await get_redis()`` to the capability didn't
silently regress the dedup behavior.
"""
from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from app.api.middleware import lane_var
from app.capabilities import redis as redis_cap_mod
from app.infra import redis as redis_infra_mod
from app.infra.image import ImageRegistry


@pytest.fixture
async def fake_redis(monkeypatch: pytest.MonkeyPatch) -> fakeredis.aioredis.FakeRedis:
    """Inject fakeredis into both ``app.infra.redis._redis`` (the raw
    singleton) and reset the capability singleton so the next
    ``get_redis_capability()`` call rebuilds against fakeredis."""
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_infra_mod, "_redis", fake)
    monkeypatch.setattr(redis_cap_mod, "_singleton", None)
    return fake


# ---------------------------------------------------------------------------
# Basic round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_assigns_sequential_filenames(fake_redis):
    """First three registrations get 1.png / 2.png / 3.png."""
    reg = ImageRegistry("msg-1")
    assert await reg.register("https://tos/a") == "1.png"
    assert await reg.register("https://tos/b") == "2.png"
    assert await reg.register("https://tos/c") == "3.png"


@pytest.mark.asyncio
async def test_resolve_returns_registered_url(fake_redis):
    reg = ImageRegistry("msg-2")
    await reg.register("https://tos/x")
    assert await reg.resolve("1.png") == "https://tos/x"


@pytest.mark.asyncio
async def test_resolve_missing_returns_none(fake_redis):
    reg = ImageRegistry("msg-empty")
    assert await reg.resolve("nope.png") is None


@pytest.mark.asyncio
async def test_resolve_all_excludes_counter(fake_redis):
    reg = ImageRegistry("msg-3")
    await reg.register("https://tos/a")
    await reg.register("https://tos/b")
    all_ = await reg.resolve_all()
    assert all_ == {"1.png": "https://tos/a", "2.png": "https://tos/b"}
    assert "__counter__" not in all_


@pytest.mark.asyncio
async def test_register_batch_assigns_sequential(fake_redis):
    reg = ImageRegistry("msg-batch")
    names = await reg.register_batch(
        ["https://tos/a", "https://tos/b", "https://tos/c"]
    )
    assert names == ["1.png", "2.png", "3.png"]


@pytest.mark.asyncio
async def test_register_batch_empty_returns_empty(fake_redis):
    reg = ImageRegistry("msg-batch-empty")
    assert await reg.register_batch([]) == []


# ---------------------------------------------------------------------------
# C5 acceptance: two lanes concurrent registration, independent state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_lanes_concurrent_dedup_independent(fake_redis):
    """C5 acceptance: registering against ``message_id="m"`` from two
    different lanes must produce two independent counters."""

    async def register_in_lane(lane: str, url_prefix: str) -> list[str]:
        token = lane_var.set(lane)
        try:
            reg = ImageRegistry("shared-msg")
            names: list[str] = []
            for i in range(4):
                n = await reg.register(f"{url_prefix}/{i}")
                names.append(n)
                # let the other coroutine interleave
                await asyncio.sleep(0)
            return names
        finally:
            lane_var.reset(token)

    res_ppe, res_coe = await asyncio.gather(
        register_in_lane("ppe-one", "https://tos/ppe"),
        register_in_lane("coe-two", "https://tos/coe"),
    )

    # Each lane sees its own 1..4 sequence — counters are independent.
    assert res_ppe == ["1.png", "2.png", "3.png", "4.png"]
    assert res_coe == ["1.png", "2.png", "3.png", "4.png"]

    # Underlying keys are lane-prefixed; the bare key is empty.
    ppe_hash = await fake_redis.hgetall("ppe-one:image_registry:shared-msg")
    coe_hash = await fake_redis.hgetall("coe-two:image_registry:shared-msg")
    bare = await fake_redis.hgetall("image_registry:shared-msg")
    assert ppe_hash["1.png"] == "https://tos/ppe/0"
    assert coe_hash["1.png"] == "https://tos/coe/0"
    assert bare == {}


@pytest.mark.asyncio
async def test_resolve_isolated_per_lane(fake_redis):
    """A URL registered in lane A is invisible from lane B for the same
    logical ``message_id``."""
    # Register in ppe-a
    token = lane_var.set("ppe-a")
    try:
        reg_a = ImageRegistry("msg-iso")
        await reg_a.register("https://tos/from-a")
    finally:
        lane_var.reset(token)

    # Look it up from coe-b — should miss
    token = lane_var.set("coe-b")
    try:
        reg_b = ImageRegistry("msg-iso")
        assert await reg_b.resolve("1.png") is None
        assert await reg_b.resolve_all() == {}
    finally:
        lane_var.reset(token)

    # And from ppe-a, the original is still there
    token = lane_var.set("ppe-a")
    try:
        reg_a = ImageRegistry("msg-iso")
        assert await reg_a.resolve("1.png") == "https://tos/from-a"
    finally:
        lane_var.reset(token)
