"""Tests for capabilities.banned_words (Phase 7d Gap 14).

Uses ``fakeredis[lua]`` to back ``app.infra.redis.get_redis()`` with an
in-memory Redis stub.
"""
from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.capabilities import banned_words


@pytest.fixture
async def fake_redis(monkeypatch: pytest.MonkeyPatch) -> fakeredis.aioredis.FakeRedis:
    import app.capabilities.redis as redis_cap_mod
    import app.infra.redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis", fake)
    # Reset capability singleton so it rebuilds against fakeredis.
    monkeypatch.setattr(redis_cap_mod, "_singleton", None)
    return fake


@pytest.mark.asyncio
async def test_contains_clean_text(fake_redis):
    await fake_redis.sadd("banned_words", "badword1", "另一个屏蔽词")
    assert await banned_words.contains("hello world") is None


@pytest.mark.asyncio
async def test_contains_hit(fake_redis):
    await fake_redis.sadd("banned_words", "badword1")
    assert await banned_words.contains("this contains badword1 inside") == "badword1"


@pytest.mark.asyncio
async def test_contains_chinese_normalization(fake_redis):
    await fake_redis.sadd("banned_words", "另一个屏蔽词")
    # 中间空格被剔除（normalize），lower 不影响中文
    assert (
        await banned_words.contains("中间有 另一个屏蔽词 的句子") == "另一个屏蔽词"
    )


@pytest.mark.asyncio
async def test_contains_empty_set(fake_redis):
    assert await banned_words.contains("anything") is None


@pytest.mark.asyncio
async def test_contains_lowercases_input(fake_redis):
    """Existing _check_banned_word matched 'BADWORD' against banned 'badword'."""
    await fake_redis.sadd("banned_words", "badword")
    assert await banned_words.contains("HELLO BADWORD WORLD") == "badword"
