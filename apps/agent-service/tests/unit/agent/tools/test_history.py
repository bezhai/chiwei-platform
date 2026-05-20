"""Tests for app.agent.tools.history — chat history and group member tools."""

from contextlib import asynccontextmanager
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
# tx() stub for tests — Phase 7d Task 4: business code now uses
# `async with tx():` instead of `async with get_session() as s:`
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _fake_tx():
    yield


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
            patch("app.agent.tools.history.tx", _fake_tx),
            patch(
                "app.data.queries.find_messages_in_range",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
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
            patch("app.agent.tools.history.tx", _fake_tx),
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
            result = await check_chat_history.coroutine("新番")
            assert "新番" in result


    @pytest.mark.asyncio
    async def test_user_speaker_reads_row_username_not_find_username(self):
        """身份全局化：user 行说话人显示名直接读 msg.username 行级冗余列，
        不再按全局 user_id 调 find_username（行级本意：这条消息当时的发送
        者名，而非该 user 最近一条非空名）。"""
        from app.agent.tools.history import check_chat_history

        mock_context = MagicMock()
        mock_context.context = AgentContext(chat_id="test_chat")

        msg = MagicMock(
            create_time=1712000000000,
            role="user",
            user_id="internal_user_42",
            username="行级当时名",
            content='{"text":"hello"}',
        )
        mock_parsed = MagicMock()
        mock_parsed.render.return_value = "hello"

        def _boom(*_a, **_kw):  # find_username 不该被这个消费点调用
            raise AssertionError(
                "check_chat_history 不该再为显示名调 find_username"
            )

        with (
            patch("app.agent.tools.history.get_runtime", return_value=mock_context),
            patch("app.agent.tools.history.tx", _fake_tx),
            patch(
                "app.data.queries.find_messages_in_range",
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch("app.data.queries.find_username", side_effect=_boom),
            patch(
                "app.chat.content_parser.parse_content",
                return_value=mock_parsed,
            ),
        ):
            result = await check_chat_history.coroutine("hello")
            assert "行级当时名" in result

    @pytest.mark.asyncio
    async def test_user_speaker_falls_to_question_mark_when_row_username_empty(self):
        """msg.username 为空 → 退到 '?'（与原 name or '?' 行为一致），
        但来源是行级列，不是 find_username。"""
        from app.agent.tools.history import check_chat_history

        mock_context = MagicMock()
        mock_context.context = AgentContext(chat_id="test_chat")

        msg = MagicMock(
            create_time=1712000000000,
            role="user",
            user_id="internal_user_42",
            username=None,
            content='{"text":"world"}',
        )
        mock_parsed = MagicMock()
        mock_parsed.render.return_value = "world"

        with (
            patch("app.agent.tools.history.get_runtime", return_value=mock_context),
            patch("app.agent.tools.history.tx", _fake_tx),
            patch(
                "app.data.queries.find_messages_in_range",
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(
                "app.chat.content_parser.parse_content",
                return_value=mock_parsed,
            ),
        ):
            result = await check_chat_history.coroutine("world")
            assert "?: world" in result


# ---------------------------------------------------------------------------
# search_group_history — assistant 说话人按 role 派生（与本文件 '我' 一致）
# ---------------------------------------------------------------------------


class TestSearchGroupHistorySpeaker:
    @pytest.mark.asyncio
    async def test_assistant_row_shows_role_derived_name_not_username(self):
        """assistant 行不能直接 username or '?'（username 对 assistant 行
        本就为空 → 全显示 '?'）。按 role 派生，与 check_chat_history 的
        assistant 显示风格 '我' 一致；user 行仍读 username。"""
        from app.agent.tools import history

        mock_context = MagicMock()
        mock_context.context = AgentContext(chat_id="test_chat")

        assistant_msg = MagicMock(
            create_time=1712000000000,
            role="assistant",
            message_id="a1",
            content='{"text":"我的回复"}',
        )
        user_msg = MagicMock(
            create_time=1712000060000,
            role="user",
            message_id="u1",
            content='{"text":"用户的话"}',
        )

        parsed_a = MagicMock()
        parsed_a.render.return_value = "我的回复"
        parsed_u = MagicMock()
        parsed_u.render.return_value = "用户的话"

        fake_embedding = MagicMock()
        fake_embedding.dense = [0.1]
        fake_embedding.sparse = MagicMock(indices=[1], values=[1.0])

        fake_qdrant = MagicMock()
        fake_qdrant.hybrid_search = AsyncMock(
            return_value=[
                {"payload": {"message_id": "a1", "timestamp": 1712000000000}}
            ]
        )

        with (
            patch.object(
                history, "get_runtime", return_value=mock_context
            ),
            patch(
                "app.agent.embedding.embed_hybrid",
                new_callable=AsyncMock,
                return_value=fake_embedding,
            ),
            patch("app.infra.qdrant.qdrant", fake_qdrant),
            patch(
                "app.data.queries.find_context_messages_for_anchors",
                new_callable=AsyncMock,
                return_value=[(assistant_msg, None), (user_msg, "小明")],
            ),
            patch(
                "app.chat.content_parser.parse_content",
                side_effect=[parsed_a, parsed_u],
            ),
        ):
            result = await history.search_group_history.coroutine("回想")

        # assistant 行：按 role 派生，显示 '我'（与本文件既有风格一致），
        # 绝不能是 '?'
        assert "我: 我的回复" in result
        # user 行仍读 username
        assert "小明: 用户的话" in result
        # assistant 行不再退化成 '?'
        assert "?: 我的回复" not in result


# ---------------------------------------------------------------------------
# search_group_history — Qdrant chat_id 过滤的全局 ID 契约 (T5-5c)
#
# 写入端 vectorize-worker 把 conversation_messages 行（user_id/chat_id 已是
# 全局 internal_*_id，由 channel-server 入站契约链 resolve 后落库）原样塞进
# messages_recall 的 payload。读取端 search_group_history 用 context.chat_id
# 过滤——而 context.chat_id 来自 quick_search → conversation_messages 行的
# chat_id，同样是全局 internal_chat_id。两端对称，filter 必须：
#   - key 恒为 "chat_id"（与 payload 字段名一致，不按渠道改名）
#   - value 恒等于 context.chat_id 原值（全局 internal_chat_id 直传，
#     不做任何飞书裸 ID 反查 / 转换 / fallback）
# 本测试钉死这条对称契约，防止有人偷偷加旧飞书 ID 兼容分支。
# ---------------------------------------------------------------------------


class TestSearchGroupHistoryQdrantFilterContract:
    @pytest.mark.asyncio
    async def test_qdrant_filter_uses_global_chat_id_verbatim(self):
        from app.agent.tools import history

        global_chat_id = "01J8XGLOBALCHATID0000000000"
        mock_context = MagicMock()
        mock_context.context = AgentContext(chat_id=global_chat_id)

        fake_embedding = MagicMock()
        fake_embedding.dense = [0.1]
        fake_embedding.sparse = MagicMock(indices=[1], values=[1.0])

        fake_qdrant = MagicMock()
        fake_qdrant.hybrid_search = AsyncMock(return_value=[])

        with (
            patch.object(history, "get_runtime", return_value=mock_context),
            patch(
                "app.agent.embedding.embed_hybrid",
                new_callable=AsyncMock,
                return_value=fake_embedding,
            ),
            patch("app.infra.qdrant.qdrant", fake_qdrant),
        ):
            await history.search_group_history.coroutine("回想")

        fake_qdrant.hybrid_search.assert_awaited_once()
        query_filter = fake_qdrant.hybrid_search.await_args.kwargs[
            "query_filter"
        ]
        # 只有一个 must 条件，key 恒为 "chat_id"
        assert len(query_filter.must) == 1
        cond = query_filter.must[0]
        assert cond.key == "chat_id"
        # value 恒等于 context.chat_id 原值（全局 internal_chat_id 直传，
        # 没有任何飞书裸 ID 反查 / 转换 / fallback）
        assert cond.match.value == global_chat_id


# ---------------------------------------------------------------------------
# list_group_members
# ---------------------------------------------------------------------------


class TestListGroupMembers:
    @pytest.mark.asyncio
    async def test_empty_group(self):
        from app.agent.tools.history import list_group_members

        mock_context = MagicMock()
        mock_context.context = AgentContext(chat_id="test_chat")

        with (
            patch("app.agent.tools.history.get_runtime", return_value=mock_context),
            patch(
                "app.data.queries.find_group_members",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
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

        with (
            patch("app.agent.tools.history.get_runtime", return_value=mock_context),
            patch(
                "app.data.queries.find_group_members",
                new_callable=AsyncMock,
                return_value=[(member1, user1), (member2, user2), (member3, user3)],
            ),
        ):
            result = await list_group_members.coroutine()
            assert "3人" in result
            assert "[群主]" in result
            assert "[管理员]" in result
            assert "Owner" in result
            assert "Normal" in result
