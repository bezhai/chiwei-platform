"""Common-layer sender display-name query contract.

agent-service 只消费 common_*：``find_username`` 读 ``common_user``，
上下文消息显示名读 ``common_message.sender_display_name``。测试只断言 common
表契约，不能重新引入 lark/private 表。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.data.queries.messages import (
    find_username,
)


class _ScalarResult:
    def __init__(self, value, rows=None):
        self.value = value
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self._rows


USER_ID = "00000000-0000-7000-8000-000000000001"
CHAT_ID = "00000000-0000-7000-8000-000000000002"
MSG_ID = "00000000-0000-7000-8000-000000000003"


class _FakeMsg:
    def __init__(self, *, message_id: str, sender_display_name: str | None):
        self.common_message_id = message_id
        self.common_user_id = USER_ID
        self.sender_display_name = sender_display_name
        self.content = [{"type": "text", "value": "hello"}]
        self.content_text = "hello"
        self.role = "user"
        self.common_root_message_id = message_id
        self.common_reply_message_id = None
        self.common_conversation_id = CHAT_ID
        self.scope = "group"
        self.event_time = 1000
        self.message_type = "text"
        self.bot_name = None
        self.response_id = None


@asynccontextmanager
async def _fake_auto_tx():
    yield


def _patch_session(session):
    return [
        patch("app.data.queries.messages.auto_tx", _fake_auto_tx),
        patch("app.data.queries.messages.current_session", return_value=session),
    ]


@pytest.mark.asyncio
async def test_find_username_reads_common_user_display_name():
    """find_username 直接读 common_user.display_name，无 lark/private JOIN。"""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult("Alice"))

    patches = _patch_session(session)
    for p in patches:
        p.start()
    try:
        result = await find_username(USER_ID)
        assert result == "Alice"

        session.execute.assert_awaited_once()
        sql_text = str(session.execute.await_args.args[0]).lower()
        assert "common_user.display_name" in sql_text
        assert "lark_" not in sql_text
        assert "union_id" not in sql_text
        assert "coalesce" not in sql_text
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_find_username_returns_none_when_no_row():
    """没有对应消息行 → 返回 None（不抛）。"""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(None))

    patches = _patch_session(session)
    for p in patches:
        p.start()
    try:
        assert await find_username("00000000-0000-7000-8000-000000000099") is None
    finally:
        for p in patches:
            p.stop()


