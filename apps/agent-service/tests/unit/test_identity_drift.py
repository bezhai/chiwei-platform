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
    mock_redis.hget.assert_called_once_with("reply_style:chat_001", "state")


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
        fake_hset("reply_style:chat_001", {"state": "有点困", "updated_at": "2026-03-28T15:00:00"}),
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
async def test_run_drift_calls_observer_then_generator():
    """_run_drift 先调 observer 再调 generator，保存 generator 的输出"""
    observer_response = MagicMock()
    observer_response.content = "## 情感状态\n精力低\n## 偏差诊断\n回复太长\n## 下一轮方向\n要短"

    generator_response = MagicMock()
    generator_response.content = "[精力低，懒]\n\n--- 被问问题 ---\n不知道诶\n懒得查"

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(side_effect=[observer_response, generator_response])

    mock_redis = AsyncMock()
    mock_redis.hget = AsyncMock(return_value="上一轮的 reply_style")
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
              new_callable=AsyncMock, return_value="[15:30] A: 你好\n[15:31] 赤尾: 嗯"),
        patch("app.services.identity_drift._get_recent_akao_replies",
              new_callable=AsyncMock, return_value="1. 嗯\n2. 不知道"),
        patch("app.services.identity_drift._get_schedule_context",
              new_callable=AsyncMock, return_value="下午犯困"),
    ):
        mock_redis_cls.get_instance.return_value = mock_redis
        mock_mb.build_chat_model = AsyncMock(return_value=mock_model)

        mock_observer_prompt = MagicMock()
        mock_observer_prompt.compile.return_value = "observer compiled"
        mock_generator_prompt = MagicMock()
        mock_generator_prompt.compile.return_value = "generator compiled"
        mock_get_prompt.side_effect = lambda name: (
            mock_observer_prompt if name == "drift_observer" else mock_generator_prompt
        )

        from app.services.identity_drift import _run_drift
        await _run_drift("chat_001")

    # 两次 LLM 调用
    assert mock_model.ainvoke.call_count == 2
    # get_prompt 调了 observer 和 generator
    mock_get_prompt.assert_any_call("drift_observer")
    mock_get_prompt.assert_any_call("drift_generator")
    # 保存的是 generator 的输出
    mock_pipe.hset.assert_called_once()
    call_args = mock_pipe.hset.call_args
    mapping = call_args.kwargs.get("mapping") if call_args.kwargs else call_args[1] if len(call_args.args) > 1 else None
    assert mapping is not None
    assert "精力低" in mapping["state"] or "懒" in mapping["state"]


@pytest.mark.asyncio
async def test_get_recent_akao_replies_filters_assistant_only():
    """只返回赤尾的回复，不含其他人的消息"""
    mock_messages = [
        MagicMock(role="user", content='{"text":"你好"}', create_time=1000),
        MagicMock(role="assistant", content='{"text":"你好呀～"}', create_time=2000),
        MagicMock(role="user", content='{"text":"在干嘛"}', create_time=3000),
        MagicMock(role="assistant", content='{"text":"发呆"}', create_time=4000),
        MagicMock(role="assistant", content='{"text":"不想动"}', create_time=5000),
    ]

    mock_render = MagicMock()
    mock_render.render = MagicMock(side_effect=["你好呀～", "发呆", "不想动"])

    with (
        patch("app.services.identity_drift.get_chat_messages_in_range",
              new_callable=AsyncMock, return_value=mock_messages),
        patch("app.services.identity_drift.parse_content", return_value=mock_render),
    ):
        from app.services.identity_drift import _get_recent_akao_replies
        result = await _get_recent_akao_replies("chat_001")

    # 3 条赤尾回复，编号 1-3
    assert "1. 你好呀～" in result
    assert "2. 发呆" in result
    assert "3. 不想动" in result
    # 不应包含 user 消息原文
    lines = result.strip().split("\n")
    assert len(lines) == 3


@pytest.mark.asyncio
async def test_get_base_reply_style_returns_none_when_empty():
    """无基线时返回 None"""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)

    with patch("app.services.identity_drift.AsyncRedisClient") as mock_cls:
        mock_cls.get_instance.return_value = mock_redis
        from app.services.identity_drift import get_base_reply_style

        result = await get_base_reply_style()

    assert result is None
    mock_redis.get.assert_called_once_with("reply_style:__base__")


@pytest.mark.asyncio
async def test_set_base_reply_style_stores_with_ttl():
    """写入基线并设置 TTL"""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()

    with patch("app.services.identity_drift.AsyncRedisClient") as mock_cls:
        mock_cls.get_instance.return_value = mock_redis
        from app.services.identity_drift import set_base_reply_style

        await set_base_reply_style("懒洋洋的，说话短")

    mock_redis.set.assert_called_once()
    call_args = mock_redis.set.call_args
    assert call_args[0][0] == "reply_style:__base__"
    assert call_args[0][1] == "懒洋洋的，说话短"
    # TTL: 12 小时（覆盖到下一次生成）
    assert call_args[1].get("ex") == 43200
