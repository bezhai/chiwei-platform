"""Identity 漂移状态机测试"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))


@pytest.mark.asyncio
async def test_get_identity_state_returns_none_when_empty():
    """无状态时返回 None"""
    mock_redis = AsyncMock()
    mock_redis.hget = AsyncMock(return_value=None)

    with patch("app.services.identity_drift.AsyncRedisClient") as mock_cls:
        mock_cls.get_instance.return_value = mock_redis
        from app.services.identity_drift import get_identity_state

        result = await get_identity_state("chat_001")

    assert result is None
    mock_redis.hget.assert_called_once_with("identity:chat_001", "state")


@pytest.mark.asyncio
async def test_set_and_get_identity_state():
    """写入后能读回"""
    store = {}

    async def fake_hset(key, mapping):
        store[key] = mapping

    async def fake_hget(key, field):
        return store.get(key, {}).get(field)

    async def fake_expire(key, ttl):
        pass

    mock_redis = AsyncMock()
    mock_redis.hset = fake_hset
    mock_redis.hget = fake_hget
    mock_redis.expire = fake_expire
    mock_pipe = MagicMock()
    mock_pipe.hset = MagicMock()
    mock_pipe.expire = MagicMock()
    mock_pipe.execute = AsyncMock(side_effect=lambda: [
        fake_hset("identity:chat_001", {"state": "有点困", "updated_at": "2026-03-28T15:00:00"}),
        None,
    ])
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)

    with patch("app.services.identity_drift.AsyncRedisClient") as mock_cls:
        mock_cls.get_instance.return_value = mock_redis
        from app.services.identity_drift import set_identity_state, get_identity_state

        await set_identity_state("chat_001", "有点困")

    mock_pipe.hset.assert_called_once()
    mock_pipe.expire.assert_called_once()
