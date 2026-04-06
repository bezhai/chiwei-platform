import json
from datetime import datetime, timedelta, timezone
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


@pytest.mark.asyncio
async def test_tick_skips_when_skip_until_future():
    """skip_until 在未来 → 不调用 LLM"""
    engine = LifeEngine()
    future = (datetime.now(tz=timezone(timedelta(hours=8))) + timedelta(hours=1)).isoformat()
    state = LifeState(
        current_state="在看番",
        activity_type="busy",
        response_mood="沉浸中",
        skip_until=future,
        updated_at=datetime.now(tz=timezone(timedelta(hours=8))).isoformat(),
    )
    existing_json = json.dumps(state.to_dict(), ensure_ascii=False)

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=existing_json)

    with (
        patch("app.services.life_engine.AsyncRedisClient.get_instance", return_value=mock_redis),
        patch("app.services.life_engine.LifeEngine._think", new_callable=AsyncMock) as mock_think,
    ):
        await engine.tick("akao-001")
        mock_think.assert_not_called()


@pytest.mark.asyncio
async def test_tick_calls_think_when_no_skip():
    """无 skip → 调用 LLM think"""
    engine = LifeEngine()
    state = LifeState(
        current_state="发呆",
        activity_type="idle",
        response_mood="无聊",
        skip_until=None,
        updated_at=datetime.now(tz=timezone(timedelta(hours=8))).isoformat(),
    )
    new_state = LifeState(
        current_state="去刷手机了",
        activity_type="browsing",
        response_mood="好奇",
        skip_until=None,
        updated_at=datetime.now(tz=timezone(timedelta(hours=8))).isoformat(),
    )

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=json.dumps(state.to_dict(), ensure_ascii=False))

    with (
        patch("app.services.life_engine.AsyncRedisClient.get_instance", return_value=mock_redis),
        patch("app.services.life_engine.LifeEngine._think", new_callable=AsyncMock, return_value=new_state),
        patch("app.services.life_engine.LifeEngine._on_state_change", new_callable=AsyncMock) as mock_change,
    ):
        await engine.tick("akao-001")
        mock_redis.set.assert_called_once()
        # activity changed from idle → browsing → should trigger _on_state_change
        mock_change.assert_called_once()


@pytest.mark.asyncio
async def test_tick_expired_skip_triggers_think():
    """skip_until 已过期 → 调用 LLM think"""
    engine = LifeEngine()
    past = (datetime.now(tz=timezone(timedelta(hours=8))) - timedelta(minutes=5)).isoformat()
    state = LifeState(
        current_state="刚看完番",
        activity_type="busy",
        response_mood="意犹未尽",
        skip_until=past,
        updated_at=datetime.now(tz=timezone(timedelta(hours=8))).isoformat(),
    )

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=json.dumps(state.to_dict(), ensure_ascii=False))
    new_state = LifeState(
        current_state="无聊了",
        activity_type="idle",
        response_mood="有点空虚",
        skip_until=None,
        updated_at=datetime.now(tz=timezone(timedelta(hours=8))).isoformat(),
    )

    with (
        patch("app.services.life_engine.AsyncRedisClient.get_instance", return_value=mock_redis),
        patch("app.services.life_engine.LifeEngine._think", new_callable=AsyncMock, return_value=new_state),
        patch("app.services.life_engine.LifeEngine._on_state_change", new_callable=AsyncMock),
    ):
        await engine.tick("akao-001")
        mock_redis.set.assert_called_once()
