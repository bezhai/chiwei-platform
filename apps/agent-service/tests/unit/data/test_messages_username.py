"""Identity globalization: conversation_messages.username 冗余列读取契约。

身份全局化后 conversation_messages.user_id 从飞书 union_id 变成全局
internal_user_id，原来靠 ``JOIN lark_user ON user_id = union_id`` 取显示名
的逻辑会断。本测试钉死新契约：

- ``find_username`` 直接读 ``conversation_messages.username`` 列，
  SQL 里不再出现 lark_user / union_id JOIN，也没有 COALESCE fallback。
- ``find_context_messages_for_anchors`` 返回
  ``(ConversationMessage, username_str | None)``，SQL 里不再 JOIN
  lark_user，username 直接取自 conversation_messages.username 列。

全程不连真实 DB —— session 用 AsyncMock 假装，断言落在生成的 SQL 文本
和返回值结构上（与 tests/unit/data/test_queries.py 同风格）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.data.queries.messages import (
    find_context_messages_for_anchors,
    find_username,
)


class _ScalarResult:
    def __init__(self, value, rows=None):
        self.value = value
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self.value

    def all(self):
        return self._rows


@asynccontextmanager
async def _fake_auto_tx():
    yield


def _patch_session(session):
    return [
        patch("app.data.queries.messages.auto_tx", _fake_auto_tx),
        patch("app.data.queries.messages.current_session", return_value=session),
    ]


@pytest.mark.asyncio
async def test_find_username_reads_conversation_messages_username_column():
    """find_username 直接读 conversation_messages.username，无 lark_user JOIN。"""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult("Alice"))

    patches = _patch_session(session)
    for p in patches:
        p.start()
    try:
        result = await find_username("internal_user_42")
        assert result == "Alice"

        session.execute.assert_awaited_once()
        sql_text = str(session.execute.await_args.args[0]).lower()
        # 读的是 conversation_messages.username
        assert "conversation_messages.username" in sql_text
        # 不再 JOIN lark_user / 用 union_id
        assert "lark_user" not in sql_text
        assert "union_id" not in sql_text
        # 无 COALESCE fallback
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
        assert await find_username("ghost") is None
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_find_context_messages_returns_message_and_username_string():
    """返回 (ConversationMessage, username_str)，SQL 不 JOIN lark_user。"""

    class _FakeMsg:
        message_id = "m1"
        create_time = 1000

    fake_rows = [(_FakeMsg(), "Bob"), (_FakeMsg(), None)]
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_ScalarResult(None, rows=fake_rows)
    )

    patches = _patch_session(session)
    for p in patches:
        p.start()
    try:
        out = await find_context_messages_for_anchors(
            chat_id="c1",
            anchor_message_ids=["m1"],
            anchor_timestamps=[1000],
            anchor_root_ids=set(),
        )

        # 返回结构: (msg, username_str | None)
        assert len(out) == 2
        msg0, name0 = out[0]
        msg1, name1 = out[1]
        assert name0 == "Bob"
        assert name1 is None
        assert msg0.message_id == "m1"

        sql_text = str(session.execute.await_args.args[0]).lower()
        # username 取自 conversation_messages 列，不 JOIN lark_user
        assert "lark_user" not in sql_text
        assert "union_id" not in sql_text
        assert "conversation_messages.username" in sql_text
    finally:
        for p in patches:
            p.stop()
