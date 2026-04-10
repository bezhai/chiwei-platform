"""Identity 漂移状态机测试"""

import asyncio

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.persona_loader import PersonaContext

_MOCK_PERSONA = PersonaContext(
    persona_id="akao",
    display_name="赤尾",
    persona_lite="元气活泼傲娇少女",
)


@pytest.mark.asyncio
async def test_run_drift_delegates_to_generate_voice():
    """_run_drift 拼装 recent_context 后调用 generate_voice"""
    with (
        patch("app.services.identity_drift.load_persona",
              new_callable=AsyncMock, return_value=_MOCK_PERSONA),
        patch("app.services.identity_drift._get_recent_messages",
              new_callable=AsyncMock, return_value="[15:30] A: 你好\n[15:31] 赤尾: 嗯"),
        patch("app.services.identity_drift._get_recent_persona_replies",
              new_callable=AsyncMock, return_value="1. 嗯\n2. 不知道"),
        patch("app.services.voice_generator.generate_voice",
              new_callable=AsyncMock) as mock_gen_voice,
    ):
        from app.services.identity_drift import _run_drift
        await _run_drift("chat_001", persona_id="akao")

    mock_gen_voice.assert_called_once()
    call_kwargs = mock_gen_voice.call_args
    assert call_kwargs[0][0] == "akao"  # persona_id
    assert call_kwargs[1]["source"] == "drift"
    assert "群里刚才发生的事" in call_kwargs[1]["recent_context"]
    assert "你最近的回复" in call_kwargs[1]["recent_context"]


@pytest.mark.asyncio
async def test_run_drift_skips_when_no_recent_messages():
    """近期无消息时 _run_drift 直接跳过，不调 generate_voice"""
    with (
        patch("app.services.identity_drift.load_persona",
              new_callable=AsyncMock, return_value=_MOCK_PERSONA),
        patch("app.services.identity_drift._get_recent_messages",
              new_callable=AsyncMock, return_value=""),
        patch("app.services.identity_drift._get_recent_persona_replies",
              new_callable=AsyncMock, return_value=""),
        patch("app.services.voice_generator.generate_voice",
              new_callable=AsyncMock) as mock_gen_voice,
    ):
        from app.services.identity_drift import _run_drift
        await _run_drift("chat_001", persona_id="akao")

    mock_gen_voice.assert_not_called()


@pytest.mark.asyncio
async def test_on_event_single_triggers_drift_after_debounce():
    """单个事件 -> 等待 debounce -> 执行漂移"""
    with (
        patch("app.services.identity_drift.settings") as mock_settings,
        patch("app.services.identity_drift._run_drift", new_callable=AsyncMock) as mock_drift,
    ):
        mock_settings.identity_drift_debounce_seconds = 0.1  # 100ms for test
        mock_settings.identity_drift_max_buffer = 20

        from app.services.identity_drift import IdentityDriftManager

        mgr = IdentityDriftManager()
        await mgr.on_event("chat_001", persona_id="akao")

        # Wait for debounce + small margin
        await asyncio.sleep(0.3)

        mock_drift.assert_called_once_with("chat_001", "akao")


@pytest.mark.asyncio
async def test_on_event_debounce_resets_timer():
    """多个事件在 debounce 内 -> 计时器重置 -> 只触发一次漂移"""
    with (
        patch("app.services.identity_drift.settings") as mock_settings,
        patch("app.services.identity_drift._run_drift", new_callable=AsyncMock) as mock_drift,
    ):
        mock_settings.identity_drift_debounce_seconds = 0.2
        mock_settings.identity_drift_max_buffer = 20

        from app.services.identity_drift import IdentityDriftManager

        mgr = IdentityDriftManager()

        # 3 events, each within debounce window
        await mgr.on_event("chat_001", persona_id="akao")
        await asyncio.sleep(0.05)
        await mgr.on_event("chat_001", persona_id="akao")
        await asyncio.sleep(0.05)
        await mgr.on_event("chat_001", persona_id="akao")

        # Wait for debounce from last event
        await asyncio.sleep(0.4)

        # Only one drift should fire
        mock_drift.assert_called_once_with("chat_001", "akao")


@pytest.mark.asyncio
async def test_on_event_forced_flush_at_threshold():
    """缓冲区超过 M 条 -> 强制进入二阶段"""
    with (
        patch("app.services.identity_drift.settings") as mock_settings,
        patch("app.services.identity_drift._run_drift", new_callable=AsyncMock) as mock_drift,
    ):
        mock_settings.identity_drift_debounce_seconds = 10  # long debounce
        mock_settings.identity_drift_max_buffer = 3  # low threshold for test

        from app.services.identity_drift import IdentityDriftManager

        mgr = IdentityDriftManager()

        # Send M events rapidly
        for _ in range(3):
            await mgr.on_event("chat_001", persona_id="akao")

        # Phase 2 should start immediately (no waiting for debounce)
        await asyncio.sleep(0.2)
        mock_drift.assert_called_once_with("chat_001", "akao")


@pytest.mark.asyncio
async def test_phase2_buffers_new_events():
    """二阶段执行中新事件 -> 进入下一轮缓冲区"""
    drift_started = asyncio.Event()
    drift_release = asyncio.Event()

    async def slow_drift(chat_id: str, persona_id: str):
        drift_started.set()
        await drift_release.wait()

    with (
        patch("app.services.identity_drift.settings") as mock_settings,
        patch("app.services.identity_drift._run_drift", side_effect=slow_drift) as mock_drift,
    ):
        mock_settings.identity_drift_debounce_seconds = 0.05
        mock_settings.identity_drift_max_buffer = 20

        from app.services.identity_drift import IdentityDriftManager

        mgr = IdentityDriftManager()

        # Trigger first drift
        await mgr.on_event("chat_001", persona_id="akao")
        await asyncio.sleep(0.1)  # debounce fires

        await drift_started.wait()

        # New event during phase 2
        await mgr.on_event("chat_001", persona_id="akao")
        assert mgr._buffers.get("chat_001:akao", 0) > 0  # buffered

        # Release phase 2
        drift_release.set()
        await asyncio.sleep(0.3)  # wait for next round

        # Should have been called twice (original + next round)
        assert mock_drift.call_count == 2


@pytest.mark.asyncio
async def test_get_recent_persona_replies_filters_assistant_only():
    """只返回指定 persona 的回复，不含其他人的消息"""
    mock_messages = [
        MagicMock(role="user", content='{"text":"你好"}', create_time=1000, bot_name=None),
        MagicMock(role="assistant", content='{"text":"你好呀～"}', create_time=2000, bot_name="chiwei"),
        MagicMock(role="user", content='{"text":"在干嘛"}', create_time=3000, bot_name=None),
        MagicMock(role="assistant", content='{"text":"发呆"}', create_time=4000, bot_name="chiwei"),
        MagicMock(role="assistant", content='{"text":"不想动"}', create_time=5000, bot_name="chiwei"),
    ]

    mock_render = MagicMock()
    mock_render.render = MagicMock(side_effect=["你好呀～", "发呆", "不想动"])

    with (
        patch("app.services.identity_drift.get_chat_messages_in_range",
              new_callable=AsyncMock, return_value=mock_messages),
        patch("app.services.identity_drift.parse_content", return_value=mock_render),
        patch("app.services.bot_context._resolve_bot_name_for_persona",
              new_callable=AsyncMock, return_value="chiwei"),
    ):
        from app.services.identity_drift import _get_recent_persona_replies
        result = await _get_recent_persona_replies("chat_001", persona_id="akao")

    # 3 条 persona 回复，编号 1-3
    assert "1. 你好呀～" in result
    assert "2. 发呆" in result
    assert "3. 不想动" in result
    # 不应包含 user 消息原文
    lines = result.strip().split("\n")
    assert len(lines) == 3


