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
    """Should call execute with correct filters."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    result = await find_cross_chat_messages(
        mock_session,
        user_id="user_1",
        bot_names=["chiwei"],
        exclude_chat_id="chat_current",
        allowed_group_ids=["chat_ka"],
        since_ms=1000,
    )

    assert result == []
    mock_session.execute.assert_called_once()


# --- cross_chat.py tests ---


def _make_msg(
    role: str,
    user_id: str,
    chat_id: str,
    create_time: int,
    content: str,
    chat_type: str = "group",
    bot_name: str = "chiwei",
    reply_message_id: str | None = None,
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
    msg.reply_message_id = reply_message_id
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


def test_filter_direct_interactions_excludes_unrelated_assistant_messages():
    from app.memory.cross_chat import _filter_direct_interactions

    target_user_msg = _make_msg(
        "user",
        "user_1",
        "chat_p2p_user_1",
        1000,
        '{"v":2,"text":"这是我的私聊","items":[]}',
        chat_type="p2p",
    )
    target_user_msg.message_id = "target_user_msg"

    target_reply = _make_msg(
        "assistant",
        "chiwei",
        "chat_p2p_user_1",
        2000,
        '{"v":2,"text":"只回复你","items":[]}',
        chat_type="p2p",
        reply_message_id="target_user_msg",
    )

    leaked_reply = _make_msg(
        "assistant",
        "chiwei",
        "chat_p2p_other",
        3000,
        '{"v":2,"text":"别人的私聊内容","items":[]}',
        chat_type="p2p",
        reply_message_id="other_user_msg",
    )

    result = _filter_direct_interactions(
        [target_user_msg, target_reply, leaked_reply], "user_1"
    )

    assert result == [target_user_msg, target_reply]


def test_format_interactions_output():
    from app.memory.cross_chat import _format_interactions

    msgs_chat_a = [
        _make_msg(
            "user", "u1", "chat_a", 1713160000000,
            '{"v":2,"text":"笋干烧肉好吃吗","items":[{"type":"text","value":"笋干烧肉好吃吗"}]}',
        ),
        _make_msg(
            "assistant", "bot", "chat_a", 1713160060000,
            '{"v":2,"text":"超好吃！","items":[{"type":"text","value":"超好吃！"}]}',
        ),
    ]
    msgs_chat_b = [
        _make_msg(
            "user", "u1", "chat_b", 1713160120000,
            '{"v":2,"text":"刚才那个梗太好笑了","items":[{"type":"text","value":"刚才那个梗太好笑了"}]}',
        ),
        _make_msg(
            "assistant", "bot", "chat_b", 1713160180000,
            '{"v":2,"text":"我也笑到了","items":[{"type":"text","value":"我也笑到了"}]}',
        ),
    ]
    grouped = {"chat_a": msgs_chat_a, "chat_b": msgs_chat_b}
    chat_names = {"chat_a": "粉丝群", "chat_b": "测试私聊"}

    result = _format_interactions(grouped, "测试用户A", chat_names)

    assert "粉丝群" in result
    assert "测试私聊" in result
    assert "测试用户A" in result
    assert "笋干烧肉好吃吗" in result
    assert "超好吃" in result
    assert "你" in result
    assert "最近在其他地方的互动" in result
    assert result.index("测试私聊") < result.index("粉丝群")
    assert result.index("我也笑到了") < result.index("刚才那个梗太好笑了")
    assert result.index("超好吃") < result.index("笋干烧肉好吃吗")


def test_format_interactions_empty():
    from app.memory.cross_chat import _format_interactions

    result = _format_interactions({}, "测试用户A", {})
    assert result == ""


# --- build_inner_context integration ---


@pytest.mark.asyncio
async def test_build_inner_context_includes_cross_chat():
    """build_inner_context should include cross-chat section when data exists."""
    mock_cross = "[你和 测试用户A 最近在其他地方的互动]\n\n粉丝群 · 2小时前:\n  测试用户A: 笋干好吃\n  你: 超好吃"

    with (
        patch("app.memory.context._build_life_state", return_value=""),
        patch("app.memory.context.find_latest_relationship_memory", return_value=None),
        patch("app.memory.context.find_today_fragments", return_value=[]),
        patch(
            "app.memory.cross_chat.build_cross_chat_context",
            return_value=mock_cross,
        ) as mock_build,
        patch("app.memory.context.get_session") as mock_gs,
    ):
        mock_gs.return_value.__aenter__ = AsyncMock()
        mock_gs.return_value.__aexit__ = AsyncMock()

        from app.memory.context import build_inner_context

        result = await build_inner_context(
            chat_id="chat_current",
            chat_type="p2p",
            user_ids=["user_1"],
            trigger_user_id="user_1",
            trigger_username="测试用户A",
            persona_id="akao",
        )

        assert "最近在其他地方的互动" in result
        assert "笋干好吃" in result
        mock_build.assert_called_once_with(
            persona_id="akao",
            trigger_user_id="user_1",
            trigger_username="测试用户A",
            current_chat_id="chat_current",
        )


@pytest.mark.asyncio
async def test_build_cross_chat_context_excludes_other_users_private_content():
    from app.memory.cross_chat import build_cross_chat_context

    target_user_msg = _make_msg(
        "user",
        "user_1",
        "chat_p2p_user_1",
        1000,
        '{"v":2,"text":"这是我的私聊","items":[]}',
        chat_type="p2p",
    )
    target_user_msg.message_id = "target_user_msg"

    target_reply = _make_msg(
        "assistant",
        "chiwei",
        "chat_p2p_user_1",
        2000,
        '{"v":2,"text":"只回复你","items":[]}',
        chat_type="p2p",
        reply_message_id="target_user_msg",
    )

    leaked_reply = _make_msg(
        "assistant",
        "chiwei",
        "chat_p2p_other",
        3000,
        '{"v":2,"text":"别人的私聊内容","items":[]}',
        chat_type="p2p",
        reply_message_id="other_user_msg",
    )

    with (
        patch(
            "app.memory.cross_chat.find_bot_names_for_persona",
            AsyncMock(return_value=["chiwei"]),
        ),
        patch(
            "app.memory.cross_chat.find_cross_chat_messages",
            AsyncMock(return_value=[target_user_msg, target_reply, leaked_reply]),
        ),
        patch("app.memory.cross_chat.get_session") as mock_gs,
    ):
        mock_gs.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_gs.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await build_cross_chat_context(
            persona_id="akao",
            trigger_user_id="user_1",
            trigger_username="测试用户A",
            current_chat_id="chat_current",
        )

    assert "这是我的私聊" in result
    assert "只回复你" in result
    assert "别人的私聊内容" not in result
