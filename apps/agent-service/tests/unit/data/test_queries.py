"""Tests for small query helpers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.data.queries import (
    create_pending_agent_response,
    get_safety_status,
    is_chat_request_completed,
)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value


@asynccontextmanager
async def _fake_auto_tx():
    yield


def _patch_session(session):
    """Patch auto_tx + current_session to return *session* for both query modules."""
    return [
        patch("app.data.queries.agent_response.auto_tx", _fake_auto_tx),
        patch("app.data.queries.agent_response.current_session", return_value=session),
    ]


@pytest.mark.asyncio
async def test_create_pending_agent_response_inserts_idempotent_row():
    session = AsyncMock()
    session.execute = AsyncMock()

    patches = _patch_session(session)
    for p in patches:
        p.start()
    try:
        await create_pending_agent_response(
            session_id="session-2",
            trigger_common_message_id="00000000-0000-7000-8000-000000000001",
            common_conversation_id="00000000-0000-7000-8000-000000000002",
            bot_name="bot-x",
        )
        sql, params = session.execute.call_args.args
        sql_text = str(sql)
        assert "INSERT INTO common_agent_response" in sql_text
        assert "ON CONFLICT (session_id) DO NOTHING" in sql_text
        assert params["session_id"] == "session-2"
        assert (
            params["trigger_common_message_id"]
            == "00000000-0000-7000-8000-000000000001"
        )
        assert params["common_conversation_id"] == (
            "00000000-0000-7000-8000-000000000002"
        )
        assert params["bot_name"] == "bot-x"
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "recalled"])
async def test_chat_request_completed_for_terminal_status(status):
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(status))

    patches = _patch_session(session)
    for p in patches:
        p.start()
    try:
        assert await is_chat_request_completed("session-1") is True
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "failed", "created", None])
async def test_chat_request_not_completed_for_non_terminal_status(status):
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(status))

    patches = _patch_session(session)
    for p in patches:
        p.start()
    try:
        assert await is_chat_request_completed("session-1") is False
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_chat_request_not_completed_without_session_id():
    session = AsyncMock()

    patches = _patch_session(session)
    for p in patches:
        p.start()
    try:
        assert await is_chat_request_completed(None) is False
        session.execute.assert_not_called()
    finally:
        for p in patches:
            p.stop()


# === get_safety_status ===


@pytest.mark.asyncio
async def test_get_safety_status_returns_existing_value():
    """row 存在 + status 字段有值 → 返回 status 字符串。"""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult("pending"))

    patches = _patch_session(session)
    for p in patches:
        p.start()
    try:
        result = await get_safety_status("sess-1")
        assert result == "pending"
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_get_safety_status_returns_none_when_row_missing():
    """row 不存在 → 返回 None（不抛）。"""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(None))

    patches = _patch_session(session)
    for p in patches:
        p.start()
    try:
        result = await get_safety_status("sess-does-not-exist")
        assert result is None
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_get_safety_status_uses_correct_query_and_param():
    """SELECT safety_status FROM common_agent_response WHERE session_id = :sid."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult("passed"))

    patches = _patch_session(session)
    for p in patches:
        p.start()
    try:
        await get_safety_status("sess-xyz")

        session.execute.assert_awaited_once()
        args = session.execute.await_args.args
        sql_obj = args[0]
        sql_text = str(sql_obj)
        assert "SELECT safety_status" in sql_text
        assert "common_agent_response" in sql_text
        assert "WHERE session_id = :sid" in sql_text
        assert args[1] == {"sid": "sess-xyz"}
    finally:
        for p in patches:
            p.stop()
