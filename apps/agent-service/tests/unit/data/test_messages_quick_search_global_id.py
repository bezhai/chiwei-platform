"""Identity globalization (T5-5c): quick_search 两个根/补充查询的全局 ID 契约。

quick_search.py 依赖 ``find_messages_with_user_chat_persona_by_root`` /
``find_messages_with_user_chat_persona_in_chat`` 拉同一会话消息并附带发送者
显示名。身份全局化后 ``conversation_messages.user_id`` 是全局
internal_user_id，永远不可能等于 ``lark_user.union_id``，原来的
``outerjoin(LarkUser, user_id == union_id)`` 在全局 ID 下永远不命中 →
username 永远 NULL → quick_search 把每个用户都显示成 fallback。

本测试钉死与已修的 ``find_username`` / ``find_context_messages_for_anchors``
同一套契约：

- SQL 里不再 JOIN lark_user，也不再出现 ``union_id``。
- 发送者显示名直接取 ``conversation_messages.username`` 冗余列。
- 没有 COALESCE / 旧飞书 ID fallback。

全程不连真实 DB —— session 用 AsyncMock 假装，断言落在生成的 SQL 文本
和返回结构上（与 tests/unit/data/test_messages_username.py 同风格）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.data.queries.messages import (
    find_messages_with_user_chat_persona_by_root,
    find_messages_with_user_chat_persona_in_chat,
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

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


class _FakeMsg:
    message_id = "internal_msg_1"
    create_time = 1000


@pytest.mark.asyncio
async def test_by_root_reads_username_column_no_lark_user_join():
    """根链查询：发送者名读 conversation_messages.username，无 lark_user JOIN。"""
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_Result([(_FakeMsg(), "Alice", "群A", "p1")])
    )

    patches = _patch_session(session)
    for p in patches:
        p.start()
    try:
        out = await find_messages_with_user_chat_persona_by_root(
            root_message_id="internal_root_1",
            until_create_time=2000,
        )
        assert len(out) == 1
        msg, username, chat_name, persona_id = out[0]
        assert username == "Alice"
        assert msg.message_id == "internal_msg_1"

        sql_text = str(session.execute.await_args.args[0]).lower()
        # 全局 ID 下不再 JOIN lark_user / 用 union_id
        assert "lark_user" not in sql_text
        assert "union_id" not in sql_text
        # 发送者名取自 conversation_messages.username 冗余列
        assert "conversation_messages.username" in sql_text
        # 无 COALESCE fallback
        assert "coalesce" not in sql_text
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_in_chat_reads_username_column_no_lark_user_join():
    """补充窗口查询：同一套契约 —— 无 lark_user JOIN，读 username 列。"""
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_Result([(_FakeMsg(), None, "群B", None)])
    )

    patches = _patch_session(session)
    for p in patches:
        p.start()
    try:
        out = await find_messages_with_user_chat_persona_in_chat(
            chat_id="internal_chat_1",
            exclude_root_message_id="internal_root_1",
            after_create_time=0,
            before_create_time=9999,
            exclude_user_id="__proactive__",
            limit=10,
        )
        assert len(out) == 1
        msg, username, chat_name, persona_id = out[0]
        assert username is None  # 列为空时如实 None，不退飞书 ID 反查

        sql_text = str(session.execute.await_args.args[0]).lower()
        assert "lark_user" not in sql_text
        assert "union_id" not in sql_text
        assert "conversation_messages.username" in sql_text
        assert "coalesce" not in sql_text
    finally:
        for p in patches:
            p.stop()
