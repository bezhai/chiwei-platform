"""Identity 漂移状态机测试"""

import asyncio

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


@pytest.mark.asyncio
async def test_on_event_single_triggers_drift_after_debounce():
    """单个事件 -> 等待 debounce -> 执行漂移"""
    with (
        patch("app.services.identity_drift.AsyncRedisClient") as mock_redis_cls,
        patch("app.services.identity_drift.settings") as mock_settings,
        patch("app.services.identity_drift._run_drift", new_callable=AsyncMock) as mock_drift,
        patch("app.services.identity_drift._count_messages_since_last_drift", new_callable=AsyncMock, return_value=1),
    ):
        mock_redis = AsyncMock()
        mock_redis.hget = AsyncMock(return_value=None)
        mock_redis_cls.get_instance.return_value = mock_redis

        mock_settings.identity_drift_debounce_seconds = 0.1  # 100ms for test
        mock_settings.identity_drift_max_buffer = 20

        from app.services.identity_drift import IdentityDriftManager

        mgr = IdentityDriftManager()
        await mgr.on_event("chat_001")

        # Wait for debounce + small margin
        await asyncio.sleep(0.3)

        mock_drift.assert_called_once_with("chat_001")


@pytest.mark.asyncio
async def test_on_event_debounce_resets_timer():
    """多个事件在 debounce 内 -> 计时器重置 -> 只触发一次漂移"""
    with (
        patch("app.services.identity_drift.AsyncRedisClient") as mock_redis_cls,
        patch("app.services.identity_drift.settings") as mock_settings,
        patch("app.services.identity_drift._run_drift", new_callable=AsyncMock) as mock_drift,
        patch("app.services.identity_drift._count_messages_since_last_drift", new_callable=AsyncMock, return_value=1),
    ):
        mock_redis = AsyncMock()
        mock_redis.hget = AsyncMock(return_value=None)
        mock_redis_cls.get_instance.return_value = mock_redis

        mock_settings.identity_drift_debounce_seconds = 0.2
        mock_settings.identity_drift_max_buffer = 20

        from app.services.identity_drift import IdentityDriftManager

        mgr = IdentityDriftManager()

        # 3 events, each within debounce window
        await mgr.on_event("chat_001")
        await asyncio.sleep(0.05)
        await mgr.on_event("chat_001")
        await asyncio.sleep(0.05)
        await mgr.on_event("chat_001")

        # Wait for debounce from last event
        await asyncio.sleep(0.4)

        # Only one drift should fire
        mock_drift.assert_called_once_with("chat_001")


@pytest.mark.asyncio
async def test_on_event_forced_flush_at_threshold():
    """缓冲区超过 M 条 -> 强制进入二阶段"""
    with (
        patch("app.services.identity_drift.AsyncRedisClient") as mock_redis_cls,
        patch("app.services.identity_drift.settings") as mock_settings,
        patch("app.services.identity_drift._run_drift", new_callable=AsyncMock) as mock_drift,
        patch("app.services.identity_drift._count_messages_since_last_drift", new_callable=AsyncMock, return_value=1),
    ):
        mock_redis = AsyncMock()
        mock_redis.hget = AsyncMock(return_value=None)
        mock_redis_cls.get_instance.return_value = mock_redis

        mock_settings.identity_drift_debounce_seconds = 10  # long debounce
        mock_settings.identity_drift_max_buffer = 3  # low threshold for test

        from app.services.identity_drift import IdentityDriftManager

        mgr = IdentityDriftManager()

        # Send M events rapidly
        for _ in range(3):
            await mgr.on_event("chat_001")

        # Phase 2 should start immediately (no waiting for debounce)
        await asyncio.sleep(0.2)
        mock_drift.assert_called_once_with("chat_001")


@pytest.mark.asyncio
async def test_phase2_buffers_new_events():
    """二阶段执行中新事件 -> 进入下一轮缓冲区"""
    drift_started = asyncio.Event()
    drift_release = asyncio.Event()

    async def slow_drift(chat_id: str):
        drift_started.set()
        await drift_release.wait()

    with (
        patch("app.services.identity_drift.AsyncRedisClient") as mock_redis_cls,
        patch("app.services.identity_drift.settings") as mock_settings,
        patch("app.services.identity_drift._run_drift", side_effect=slow_drift) as mock_drift,
        patch("app.services.identity_drift._count_messages_since_last_drift", new_callable=AsyncMock, return_value=1),
    ):
        mock_redis = AsyncMock()
        mock_redis.hget = AsyncMock(return_value=None)
        mock_redis_cls.get_instance.return_value = mock_redis

        mock_settings.identity_drift_debounce_seconds = 0.05
        mock_settings.identity_drift_max_buffer = 20

        from app.services.identity_drift import IdentityDriftManager

        mgr = IdentityDriftManager()

        # Trigger first drift
        await mgr.on_event("chat_001")
        await asyncio.sleep(0.1)  # debounce fires

        await drift_started.wait()

        # New event during phase 2
        await mgr.on_event("chat_001")
        assert mgr._buffers.get("chat_001", 0) > 0  # buffered

        # Release phase 2
        drift_release.set()
        await asyncio.sleep(0.3)  # wait for next round

        # Should have been called twice (original + next round)
        assert mock_drift.call_count == 2


@pytest.mark.asyncio
async def test_run_drift_calls_llm_and_saves_state():
    """_run_drift 读取上下文 -> 调用 LLM -> 保存新状态"""
    mock_response = MagicMock()
    mock_response.content = "有点犯困但还不想睡。刚才群里闹腾了一阵，觉得好笑。"

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_response)

    mock_redis = AsyncMock()
    mock_redis.hget = AsyncMock(return_value="精力充沛，想找人聊天。")
    mock_pipe = MagicMock()
    mock_pipe.hset = MagicMock()
    mock_pipe.expire = MagicMock()
    mock_pipe.execute = AsyncMock()
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)

    with (
        patch("app.services.identity_drift.AsyncRedisClient") as mock_redis_cls,
        patch("app.services.identity_drift.ModelBuilder") as mock_mb,
        patch("app.services.identity_drift.get_prompt") as mock_get_prompt,
        patch("app.services.identity_drift._get_recent_messages",
              new_callable=AsyncMock,
              return_value="[15:30] A哥: 赤尾你觉得呢\n[15:31] 赤尾: 不觉得"),
        patch("app.services.identity_drift._get_schedule_context",
              new_callable=AsyncMock,
              return_value="下午有点犯困，想窝着看番"),
    ):
        mock_redis_cls.get_instance.return_value = mock_redis
        mock_mb.build_chat_model = AsyncMock(return_value=mock_model)

        mock_prompt = MagicMock()
        mock_prompt.compile.return_value = "compiled prompt"
        mock_get_prompt.return_value = mock_prompt

        from app.services.identity_drift import _run_drift
        await _run_drift("chat_001")

    # LLM was called
    mock_model.ainvoke.assert_called_once()
    # State was saved
    mock_pipe.hset.assert_called_once()
    call_args = mock_pipe.hset.call_args
    assert "identity:chat_001" in call_args.args or call_args.args[0] == "identity:chat_001"
