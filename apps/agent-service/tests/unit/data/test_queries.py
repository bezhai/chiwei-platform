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
