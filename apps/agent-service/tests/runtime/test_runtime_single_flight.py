"""Tests for runtime.single_flight (Gap 14).

Uses ``fakeredis[lua]`` to exercise SETNX + Lua compare-and-delete in-memory.
fakeredis honors ``ex=`` (TTL) so the ``token_compare_prevents_misdelete``
test is deterministic without network IO.
"""
from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from app.runtime.single_flight import SingleFlightConflict, single_flight


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> fakeredis.aioredis.FakeRedis:
    """Swap ``app.infra.redis._redis`` with an in-memory FakeRedis.

    ``get_redis()`` short-circuits when ``_redis`` is non-None, so injecting
    here keeps the production lazy-singleton shape intact for real runs.
    """
    import app.infra.redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis", fake)
    return fake


@pytest.mark.asyncio
async def test_acquire_releases_on_exit(fake_redis):
    async with single_flight("test:sf:basic", ttl=10):
        pass
    # 离开后能再次拿到
    async with single_flight("test:sf:basic", ttl=10):
        pass


@pytest.mark.asyncio
async def test_concurrent_acquire_raises(fake_redis):
    async def hold(latch: asyncio.Event):
        async with single_flight("test:sf:contend", ttl=10):
            latch.set()
            await asyncio.sleep(0.5)

    latch = asyncio.Event()
    holder = asyncio.create_task(hold(latch))
    await latch.wait()

    with pytest.raises(SingleFlightConflict, match="test:sf:contend"):
        async with single_flight("test:sf:contend", ttl=10):
            pass

    await holder


@pytest.mark.asyncio
async def test_token_compare_prevents_misdelete(fake_redis):
    """Slow holder past TTL doesn't delete a new holder's lock."""
    async def slow():
        async with single_flight("test:sf:ttl", ttl=1):
            await asyncio.sleep(2)  # 超过 TTL

    holder = asyncio.create_task(slow())
    await asyncio.sleep(1.2)

    # 现在新 holder 能进
    async with single_flight("test:sf:ttl", ttl=10):
        await asyncio.sleep(1)

    await holder
    async with single_flight("test:sf:ttl", ttl=10):
        pass
