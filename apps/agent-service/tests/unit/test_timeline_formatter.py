"""TimelineFormatter 单元测试

覆盖场景：
- 基本格式化（user + assistant 消息）
- with_ids 模式（输出 #id 前缀）
- max_messages 截断（只保留最近 N 条）
- 空内容跳过
- 自定义 username_resolver
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "app.services.timeline_formatter"
CST = timezone(timedelta(hours=8))


def _make_msg(
    *,
    role: str = "user",
    user_id: str = "u1",
    content: str = "hello",
    create_time: int | None = None,
    msg_id: int | None = None,
):
    """创建 mock ConversationMessage"""
    msg = MagicMock()
    msg.role = role
    msg.user_id = user_id
    msg.content = content
    msg.create_time = create_time or int(
        datetime(2026, 4, 10, 14, 30, tzinfo=CST).timestamp() * 1000
    )
    msg.id = msg_id
    return msg


@pytest.mark.asyncio
async def test_basic_formatting():
    """2 条消息（user + assistant），验证格式 [HH:MM] speaker: content"""
    user_msg = _make_msg(
        role="user",
        user_id="u1",
        content="你好",
        create_time=int(datetime(2026, 4, 10, 14, 30, tzinfo=CST).timestamp() * 1000),
    )
    bot_msg = _make_msg(
        role="assistant",
        user_id="bot",
        content="嗨～",
        create_time=int(datetime(2026, 4, 10, 14, 31, tzinfo=CST).timestamp() * 1000),
    )

    mock_parsed = MagicMock()
    mock_parsed.render = MagicMock(side_effect=["你好", "嗨～"])

    with (
        patch(f"{MODULE}.get_username", new_callable=AsyncMock, return_value="小明"),
        patch(f"{MODULE}.parse_content", return_value=mock_parsed),
    ):
        from app.services.timeline_formatter import format_timeline

        result = await format_timeline([user_msg, bot_msg], "赤尾", tz=CST)

    lines = result.strip().split("\n")
    assert len(lines) == 2
    assert "[14:30] 小明: 你好" == lines[0]
    assert "[14:31] 赤尾: 嗨～" == lines[1]


@pytest.mark.asyncio
async def test_with_ids():
    """with_ids=True 时输出 #id 前缀"""
    msg = _make_msg(
        role="user",
        user_id="u1",
        content="test",
        msg_id=42,
        create_time=int(datetime(2026, 4, 10, 15, 0, tzinfo=CST).timestamp() * 1000),
    )

    mock_parsed = MagicMock()
    mock_parsed.render = MagicMock(return_value="test")

    with (
        patch(f"{MODULE}.get_username", new_callable=AsyncMock, return_value="小明"),
        patch(f"{MODULE}.parse_content", return_value=mock_parsed),
    ):
        from app.services.timeline_formatter import format_timeline

        result = await format_timeline([msg], "赤尾", with_ids=True, tz=CST)

    assert result == "#42 [15:00] 小明: test"


@pytest.mark.asyncio
async def test_max_messages_truncation():
    """10 条消息，max_messages=3，只保留最近 3 条"""
    msgs = [
        _make_msg(
            role="user",
            user_id="u1",
            content=f"msg-{i}",
            create_time=int(
                datetime(2026, 4, 10, 14, i, tzinfo=CST).timestamp() * 1000
            ),
        )
        for i in range(10)
    ]

    mock_parsed = MagicMock()
    # 10 messages but only last 3 will be formatted
    mock_parsed.render = MagicMock(side_effect=["msg-7", "msg-8", "msg-9"])

    with (
        patch(f"{MODULE}.get_username", new_callable=AsyncMock, return_value="小明"),
        patch(f"{MODULE}.parse_content", return_value=mock_parsed),
    ):
        from app.services.timeline_formatter import format_timeline

        result = await format_timeline(msgs, "赤尾", max_messages=3, tz=CST)

    lines = result.strip().split("\n")
    assert len(lines) == 3
    assert "msg-7" in lines[0]
    assert "msg-8" in lines[1]
    assert "msg-9" in lines[2]


@pytest.mark.asyncio
async def test_empty_content_skipped():
    """空内容和纯空白消息被跳过"""
    msg_empty = _make_msg(role="user", user_id="u1", content="")
    msg_whitespace = _make_msg(role="user", user_id="u2", content="   ")
    msg_normal = _make_msg(role="user", user_id="u3", content="valid")

    mock_parsed = MagicMock()
    mock_parsed.render = MagicMock(side_effect=["", "   ", "valid"])

    with (
        patch(f"{MODULE}.get_username", new_callable=AsyncMock, return_value="小明"),
        patch(f"{MODULE}.parse_content", return_value=mock_parsed),
    ):
        from app.services.timeline_formatter import format_timeline

        result = await format_timeline(
            [msg_empty, msg_whitespace, msg_normal], "赤尾", tz=CST
        )

    lines = result.strip().split("\n")
    assert len(lines) == 1
    assert "valid" in lines[0]


@pytest.mark.asyncio
async def test_custom_username_resolver():
    """传入自定义 username_resolver 替代默认 get_username"""
    msg = _make_msg(
        role="user",
        user_id="u1",
        content="hello",
        create_time=int(datetime(2026, 4, 10, 14, 0, tzinfo=CST).timestamp() * 1000),
    )

    custom_resolver = AsyncMock(return_value="自定义名字")
    mock_parsed = MagicMock()
    mock_parsed.render = MagicMock(return_value="hello")

    with patch(f"{MODULE}.parse_content", return_value=mock_parsed):
        from app.services.timeline_formatter import format_timeline

        result = await format_timeline(
            [msg], "赤尾", tz=CST, username_resolver=custom_resolver
        )

    custom_resolver.assert_awaited_once_with("u1")
    assert "自定义名字" in result


@pytest.mark.asyncio
async def test_user_id_fallback_when_no_username():
    """get_username 返回 None 时，使用 user_id[:6] 作为 fallback"""
    msg = _make_msg(
        role="user",
        user_id="abcdefghij",
        content="hello",
        create_time=int(datetime(2026, 4, 10, 14, 0, tzinfo=CST).timestamp() * 1000),
    )

    mock_parsed = MagicMock()
    mock_parsed.render = MagicMock(return_value="hello")

    with (
        patch(f"{MODULE}.get_username", new_callable=AsyncMock, return_value=None),
        patch(f"{MODULE}.parse_content", return_value=mock_parsed),
    ):
        from app.services.timeline_formatter import format_timeline

        result = await format_timeline([msg], "赤尾", tz=CST)

    assert "abcdef" in result


@pytest.mark.asyncio
async def test_content_truncated_to_200_chars():
    """内容超过 200 字符时被截断"""
    long_content = "a" * 300
    msg = _make_msg(role="user", user_id="u1", content=long_content)

    mock_parsed = MagicMock()
    mock_parsed.render = MagicMock(return_value=long_content)

    with (
        patch(f"{MODULE}.get_username", new_callable=AsyncMock, return_value="小明"),
        patch(f"{MODULE}.parse_content", return_value=mock_parsed),
    ):
        from app.services.timeline_formatter import format_timeline

        result = await format_timeline([msg], "赤尾", tz=CST)

    # 格式: [HH:MM] 小明: aaa...aaa (200 chars)
    # 内容部分最多 200 字符
    content_part = result.split(": ", 1)[1]
    assert len(content_part) == 200
