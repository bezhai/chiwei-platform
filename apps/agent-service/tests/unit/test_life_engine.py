import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.life_engine import LifeState, LifeEngine

REDIS_KEY = "life_engine:akao-001"


def test_life_state_to_json_roundtrip():
    """LifeState → JSON → LifeState 无损往返"""
    state = LifeState(
        current_state="在沙发上刷手机",
        activity_type="browsing",
        response_mood="心情不错",
        skip_until=None,
        updated_at="2026-04-06T10:00:00+08:00",
    )
    data = state.to_dict()
    restored = LifeState.from_dict(data)
    assert restored.current_state == state.current_state
    assert restored.activity_type == state.activity_type
    assert restored.response_mood == state.response_mood
    assert restored.skip_until is None
    assert restored.updated_at == state.updated_at


def test_life_state_with_skip_until():
    """skip_until 正确序列化"""
    state = LifeState(
        current_state="在看番",
        activity_type="busy",
        response_mood="沉浸中",
        skip_until="2026-04-06T10:30:00+08:00",
        updated_at="2026-04-06T10:00:00+08:00",
    )
    data = state.to_dict()
    restored = LifeState.from_dict(data)
    assert restored.skip_until == "2026-04-06T10:30:00+08:00"


def test_life_state_default():
    """默认状态创建"""
    state = LifeState.default()
    assert state.activity_type == "idle"
    assert state.current_state  # non-empty
    assert state.response_mood  # non-empty


@pytest.mark.asyncio
async def test_save_and_load_state():
    """Redis 存取往返"""
    engine = LifeEngine()
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)

    with patch("app.services.life_engine.AsyncRedisClient.get_instance", return_value=mock_redis):
        # load when empty → default
        state = await engine._load_state("akao-001")
        assert state.activity_type == "idle"

        # save
        state.activity_type = "browsing"
        await engine._save_state("akao-001", state)
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        assert call_args[0][0] == REDIS_KEY
        saved = json.loads(call_args[0][1])
        assert saved["activity_type"] == "browsing"


@pytest.mark.asyncio
async def test_load_existing_state():
    """从 Redis 加载已有状态"""
    engine = LifeEngine()
    existing = json.dumps({
        "current_state": "在睡觉",
        "activity_type": "sleeping",
        "response_mood": "zzz",
        "skip_until": "2026-04-06T07:00:00+08:00",
        "updated_at": "2026-04-06T02:00:00+08:00",
    })
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=existing)

    with patch("app.services.life_engine.AsyncRedisClient.get_instance", return_value=mock_redis):
        state = await engine._load_state("akao-001")
        assert state.activity_type == "sleeping"
        assert state.current_state == "在睡觉"
