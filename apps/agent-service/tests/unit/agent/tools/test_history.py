"""Tests for app.agent.tools.history — chat history and group member tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.context import AgentContext

# ---------------------------------------------------------------------------
# _parse_time_hint
# ---------------------------------------------------------------------------


class TestParseTimeHint:
    def test_empty_default(self):
        from app.agent.tools.history import LOOKBACK_HOURS, _parse_time_hint

        assert _parse_time_hint("") == LOOKBACK_HOURS

    def test_yesterday(self):
        from app.agent.tools.history import _parse_time_hint

        assert _parse_time_hint("昨天") == 48

    def test_day_before(self):
        from app.agent.tools.history import _parse_time_hint

        assert _parse_time_hint("前天") == 72

    def test_today_variants(self):
        from app.agent.tools.history import _parse_time_hint

        assert _parse_time_hint("今天") == 12
        assert _parse_time_hint("刚才") == 12
        assert _parse_time_hint("上午") == 12
        assert _parse_time_hint("下午") == 12

    def test_unknown_default(self):
        from app.agent.tools.history import LOOKBACK_HOURS, _parse_time_hint

        assert _parse_time_hint("三周前") == LOOKBACK_HOURS


# ---------------------------------------------------------------------------
# _format_timestamp / _truncate
# ---------------------------------------------------------------------------


class TestFormatHelpers:
    def test_format_timestamp(self):
        from app.agent.tools.history import _format_timestamp

        result = _format_timestamp(1712000000000)
        # Should be a date string in YYYY-MM-DD HH:MM format
        assert "-" in result
        assert ":" in result

    def test_truncate_short(self):
        from app.agent.tools.history import _truncate

        assert _truncate("hello") == "hello"

    def test_truncate_long(self):
        from app.agent.tools.history import _truncate

        text = "a" * 300
        result = _truncate(text, max_len=200)
        assert result.endswith("...")
        assert len(result) == 203  # 200 chars + "..."

    def test_truncate_normalizes_whitespace(self):
        from app.agent.tools.history import _truncate

        result = _truncate("hello  \n  world")
        assert result == "hello world"


# ---------------------------------------------------------------------------
# check_chat_history
# ---------------------------------------------------------------------------


class TestCheckChatHistory:
    @pytest.mark.asyncio
    async def test_no_messages_returns_hint(self):
        """Empty message range returns friendly text."""
        from app.agent.tools.history import check_chat_history

        mock_context = MagicMock()
        mock_context.context = AgentContext(chat_id="test_chat")

        with (
            patch("app.agent.tools.history.get_runtime", return_value=mock_context),
            patch("app.agent.tools.history.get_session") as mock_session_ctx,
            patch(
                "app.data.queries.find_messages_in_range",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            mock_session = AsyncMock()
            mock_session_ctx.return_value.__aenter__ = AsyncMock(
                return_value=mock_session
            )
            mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await check_chat_history.coroutine("test")
            assert "没有聊天记录" in result

    @pytest.mark.asyncio
    async def test_keyword_filtering(self):
        """Messages matching keywords are returned preferentially."""
        from app.agent.tools.history import check_chat_history

        mock_context = MagicMock()
        mock_context.context = AgentContext(chat_id="test_chat")

        msg1 = MagicMock(
            create_time=1712000000000,
            role="user",
            user_id="u1",
            content='{"text":"聊了新番的事"}',
        )
        msg2 = MagicMock(
            create_time=1712000060000,
            role="user",
            user_id="u1",
            content='{"text":"吃了什么"}',
        )

        mock_parsed1 = MagicMock()
        mock_parsed1.render.return_value = "聊了新番的事"
        mock_parsed2 = MagicMock()
        mock_parsed2.render.return_value = "吃了什么"

        with (
            patch("app.agent.tools.history.get_runtime", return_value=mock_context),
            patch("app.agent.tools.history.get_session") as mock_session_ctx,
            patch(
                "app.data.queries.find_messages_in_range",
                new_callable=AsyncMock,
                return_value=[msg1, msg2],
            ),
            patch(
                "app.data.queries.find_username",
                new_callable=AsyncMock,
                return_value="阿儒",
            ),
            patch(
                "app.chat.content_parser.parse_content",
                side_effect=[mock_parsed1, mock_parsed2],
            ),
        ):
            mock_session = AsyncMock()
            mock_session_ctx.return_value.__aenter__ = AsyncMock(
                return_value=mock_session
            )
            mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await check_chat_history.coroutine("新番")
            assert "新番" in result


# ---------------------------------------------------------------------------
# list_group_members
# ---------------------------------------------------------------------------


class TestListGroupMembers:
    @pytest.mark.asyncio
    async def test_empty_group(self):
        from app.agent.tools.history import list_group_members

        mock_context = MagicMock()
        mock_context.context = AgentContext(chat_id="test_chat")

        mock_result = MagicMock()
        mock_result.all.return_value = []

        with (
            patch("app.agent.tools.history.get_runtime", return_value=mock_context),
            patch("app.agent.tools.history.get_session") as mock_session_ctx,
        ):
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_session_ctx.return_value.__aenter__ = AsyncMock(
                return_value=mock_session
            )
            mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await list_group_members.coroutine()
            assert "无成员" in result

    @pytest.mark.asyncio
    async def test_formats_members_with_roles(self):
        from app.agent.tools.history import list_group_members

        mock_context = MagicMock()
        mock_context.context = AgentContext(chat_id="test_chat")

        member1 = MagicMock(is_owner=True, is_manager=False)
        user1 = MagicMock(name="Owner")
        member2 = MagicMock(is_owner=False, is_manager=True)
        user2 = MagicMock(name="Admin")
        member3 = MagicMock(is_owner=False, is_manager=False)
        user3 = MagicMock(name="Normal")

        mock_result = MagicMock()
        mock_result.all.return_value = [
            (member1, user1),
            (member2, user2),
            (member3, user3),
        ]

        with (
            patch("app.agent.tools.history.get_runtime", return_value=mock_context),
            patch("app.agent.tools.history.get_session") as mock_session_ctx,
        ):
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_session_ctx.return_value.__aenter__ = AsyncMock(
                return_value=mock_session
            )
            mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await list_group_members.coroutine()
            assert "3人" in result
            assert "[群主]" in result
            assert "[管理员]" in result
            assert "Owner" in result
            assert "Normal" in result
