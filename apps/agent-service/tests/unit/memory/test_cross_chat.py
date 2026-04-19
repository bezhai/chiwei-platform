"""Tests for cross-chat context module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.data.queries import find_bot_names_for_persona, find_cross_chat_messages


@pytest.mark.asyncio
async def test_find_bot_names_for_persona():
    """Should return all active bot_names for a persona_id."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = ["chiwei", "fly", "dev"]
    mock_session.execute.return_value = mock_result

    result = await find_bot_names_for_persona(mock_session, "akao")

    assert result == ["chiwei", "fly", "dev"]
    mock_session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_find_bot_names_for_persona_empty():
    """Should return empty list when persona has no bots."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    result = await find_bot_names_for_persona(mock_session, "nonexistent")

    assert result == []


@pytest.mark.asyncio
async def test_find_cross_chat_messages_calls_db():
    """Should call execute with correct filters (blacklist semantics)."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    result = await find_cross_chat_messages(
        mock_session,
        user_id="user_1",
        bot_names=["chiwei"],
        exclude_chat_id="chat_current",
        since_ms=1000,
        excluded_chat_ids=["oc_to_skip"],
    )

    assert result == []
    mock_session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_build_cross_chat_empty_trigger_user_id():
    """trigger_user_id=None should return '' without calling DB."""
    from app.memory.cross_chat import build_cross_chat_context

    result = await build_cross_chat_context(
        persona_id="akao",
        trigger_user_id=None,
        trigger_username="测试用户A",
        current_chat_id="chat_current",
    )

    assert result == ""


@pytest.mark.asyncio
async def test_build_cross_chat_uses_dynamic_config_excluded():
    """build_cross_chat_context should pass excluded_chat_ids from dynamic config."""
    from app.memory.cross_chat import build_cross_chat_context

    mock_messages = []

    with (
        patch(
            "app.memory.cross_chat.dynamic_config.get",
            return_value="oc_a,oc_b",
        ),
        patch(
            "app.memory.cross_chat.dynamic_config.get_int",
            return_value=10,
        ),
        patch(
            "app.memory.cross_chat.find_bot_names_for_persona",
            new=AsyncMock(return_value=["chiwei"]),
        ),
        patch(
            "app.memory.cross_chat.find_cross_chat_messages",
            new=AsyncMock(return_value=mock_messages),
        ) as mock_find,
        patch("app.memory.cross_chat.get_session") as mock_gs,
    ):
        mock_gs.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

        await build_cross_chat_context(
            persona_id="akao",
            trigger_user_id="user_1",
            trigger_username="测试用户A",
            current_chat_id="chat_current",
        )

    mock_find.assert_called_once()
    call_kwargs = mock_find.call_args.kwargs
    assert call_kwargs.get("excluded_chat_ids") == ["oc_a", "oc_b"]


# --- cross_chat.py tests ---


def _make_msg(
    role: str,
    user_id: str,
    chat_id: str,
    create_time: int,
    content: str,
    chat_type: str = "group",
    bot_name: str = "chiwei",
):
    """Create a mock ConversationMessage."""
    msg = MagicMock()
    msg.role = role
    msg.user_id = user_id
    msg.chat_id = chat_id
    msg.create_time = create_time
    msg.content = content
    msg.chat_type = chat_type
    msg.bot_name = bot_name
    msg.message_id = f"msg_{create_time}"
    return msg


def test_group_and_trim_groups_by_chat():
    from app.memory.cross_chat import _group_and_trim

    msgs = [
        _make_msg("user", "u1", "chat_a", 1000, '{"v":2,"text":"hi","items":[]}'),
        _make_msg("assistant", "bot", "chat_a", 2000, '{"v":2,"text":"hello","items":[]}'),
        _make_msg("user", "u1", "chat_b", 3000, '{"v":2,"text":"hey","items":[]}', chat_type="p2p"),
    ]
    grouped = _group_and_trim(msgs, max_pairs_per_chat=10)
    assert "chat_a" in grouped
    assert "chat_b" in grouped
    assert len(grouped["chat_a"]) == 2
    assert len(grouped["chat_b"]) == 1


def test_group_and_trim_respects_limit():
    from app.memory.cross_chat import _group_and_trim

    msgs = []
    for i in range(30):
        msgs.append(
            _make_msg("user", "u1", "chat_a", i * 2000, f'{{"v":2,"text":"msg{i}","items":[]}}')
        )
        msgs.append(
            _make_msg("assistant", "bot", "chat_a", i * 2000 + 1000, f'{{"v":2,"text":"reply{i}","items":[]}}')
        )
    grouped = _group_and_trim(msgs, max_pairs_per_chat=10)
    # Should keep last 10 pairs = 20 messages
    assert len(grouped["chat_a"]) == 20


def test_format_interactions_output():
    from app.memory.cross_chat import _format_interactions

    msgs = [
        _make_msg(
            "user", "u1", "chat_a", 1713160000000,
            '{"v":2,"text":"笋干烧肉好吃吗","items":[{"type":"text","value":"笋干烧肉好吃吗"}]}',
        ),
        _make_msg(
            "assistant", "bot", "chat_a", 1713160060000,
            '{"v":2,"text":"超好吃！","items":[{"type":"text","value":"超好吃！"}]}',
        ),
    ]
    grouped = {"chat_a": msgs}
    chat_names = {"chat_a": "粉丝群"}

    result = _format_interactions(grouped, "测试用户A", chat_names)

    assert "粉丝群" in result
    assert "测试用户A" in result
    assert "笋干烧肉好吃吗" in result
    assert "超好吃" in result
    assert "你" in result
    assert "最近在其他地方的互动" in result


def test_format_interactions_empty():
    from app.memory.cross_chat import _format_interactions

    result = _format_interactions({}, "测试用户A", {})
    assert result == ""
