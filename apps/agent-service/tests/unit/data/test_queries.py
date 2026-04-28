"""Tests for small query helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.data.queries import is_chat_request_completed


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "recalled"])
async def test_chat_request_completed_for_terminal_status(status):
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(status))

    assert await is_chat_request_completed(session, "session-1") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "failed", "created", None])
async def test_chat_request_not_completed_for_non_terminal_status(status):
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(status))

    assert await is_chat_request_completed(session, "session-1") is False


@pytest.mark.asyncio
async def test_chat_request_not_completed_without_session_id():
    session = AsyncMock()

    assert await is_chat_request_completed(session, None) is False
    session.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply_count", "expected"), [(0, False), (1, True)])
async def test_proactive_request_completed_by_existing_assistant_reply(
    reply_count,
    expected,
):
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(reply_count))

    assert (
        await is_chat_request_completed(
            session,
            "session-1",
            is_proactive=True,
        )
        is expected
    )


# === get_safety_status ===

from app.data.queries import get_safety_status


@pytest.mark.asyncio
async def test_get_safety_status_returns_existing_value():
    """row 存在 + status 字段有值 → 返回 status 字符串。"""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult("pending"))

    result = await get_safety_status(session, "sess-1")
    assert result == "pending"


@pytest.mark.asyncio
async def test_get_safety_status_returns_none_when_row_missing():
    """row 不存在 → 返回 None（不抛）。"""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(None))

    result = await get_safety_status(session, "sess-does-not-exist")
    assert result is None


@pytest.mark.asyncio
async def test_get_safety_status_uses_correct_query_and_param():
    """SELECT safety_status FROM agent_responses WHERE session_id = :sid，参数 sid。"""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult("passed"))

    await get_safety_status(session, "sess-xyz")

    session.execute.assert_awaited_once()
    args = session.execute.await_args.args
    sql_obj = args[0]
    sql_text = str(sql_obj)
    assert "SELECT safety_status" in sql_text
    assert "agent_responses" in sql_text
    assert "WHERE session_id = :sid" in sql_text
    assert args[1] == {"sid": "sess-xyz"}
