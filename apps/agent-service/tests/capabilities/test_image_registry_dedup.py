"""Acceptance: ``ImageRegistry`` round-trip + cross-lane isolation
delegated to the ConfigBundle (not to the capability).

Hotfix 2026-05-13 removed the implicit ``{lane}:`` key prefix from
``RedisCapability`` (see trace 3de371aea10290b327f1386ea56f180c — the
prefix split agent-service-ppe-refactor and chat-response-worker
between two key spaces and dropped images on the floor). Lane isolation
is now physical:

* coe-* lanes get a separate Redis container (chiwei-test) via
  ConfigBundle ``class_overrides[coe]`` — two coe lanes are isolated
  because they hold two different clients.
* ppe-* lanes share prod Redis on purpose ("functional verification
  against real prod data"), so ppe-* writes are intentionally visible
  to prod readers (chat-response-worker).
"""
from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from app.capabilities import redis as redis_cap_mod
from app.capabilities.redis import RedisCapability
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
# Acceptance: coe-* lanes isolated via separate Redis backing stores
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coe_lanes_isolated_via_separate_redis_instances():
    """ConfigBundle physical isolation: two coe-* lanes point at two
    chiwei-test Redis containers. ``ImageRegistry`` against each client
    produces fully independent counters.

    The capability does NOT auto-prefix keys; isolation is purely the
    consequence of pointing at a different Redis instance, which is how
    ``class_overrides[coe]`` is supposed to work.
    """
    coe_one_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    coe_two_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cap_one = RedisCapability(coe_one_client)
    cap_two = RedisCapability(coe_two_client)

    async def register_against(cap: RedisCapability, url_prefix: str) -> list[str]:
        # ImageRegistry pulls the capability lazily via
        # ``get_redis_capability()``. Tests that want a specific client
        # call the Lua directly through the cap to mirror the same
        # script the registry would have run.
        names: list[str] = []
        key = "image_registry:shared-msg"
        from app.infra.image import _REGISTER_LUA, _REGISTRY_TTL

        for i in range(4):
            n = await cap.eval(
                _REGISTER_LUA,
                keys=[key],
                args=[f"{url_prefix}/{i}", _REGISTRY_TTL],
            )
            names.append(f"{int(n)}.png")
            await asyncio.sleep(0)
        return names

    res_one, res_two = await asyncio.gather(
        register_against(cap_one, "https://tos/one"),
        register_against(cap_two, "https://tos/two"),
    )

    assert res_one == ["1.png", "2.png", "3.png", "4.png"]
    assert res_two == ["1.png", "2.png", "3.png", "4.png"]

    one_hash = await coe_one_client.hgetall("image_registry:shared-msg")
    two_hash = await coe_two_client.hgetall("image_registry:shared-msg")
    assert one_hash["1.png"] == "https://tos/one/0"
    assert two_hash["1.png"] == "https://tos/two/0"


@pytest.mark.asyncio
async def test_ppe_lane_shares_prod_key_space(fake_redis, monkeypatch):
    """Contract: ppe-* lanes deliberately share prod Redis. An image
    written from ppe-* is visible to a prod reader looking up the same
    bare key — that's the whole point of ppe ("verify against real prod
    data").

    Failure of this contract is what dropped images in trace
    3de371aea10290b327f1386ea56f180c — the prior auto-prefix put the
    ppe entry under ``ppe-foo:image_registry:...`` while
    chat-response-worker read ``image_registry:...``.
    """
    from app.api.middleware import lane_var

    # Write from a ppe lane.
    token = lane_var.set("ppe-share")
    try:
        reg = ImageRegistry("msg-ppe-share")
        await reg.register("https://tos/from-ppe")
    finally:
        lane_var.reset(token)

    # Read with no lane set (i.e. prod), same logical key — should hit.
    reg_prod = ImageRegistry("msg-ppe-share")
    assert await reg_prod.resolve("1.png") == "https://tos/from-ppe"

    # And the raw key is the bare prod key.
    bare = await fake_redis.hgetall("image_registry:msg-ppe-share")
    assert bare["1.png"] == "https://tos/from-ppe"
