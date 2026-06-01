"""quick_search message query contract on the common layer.

agent-service 的 quick_search 只读 ``common_message`` /
``common_conversation`` / ``common_agent_response``。发送者名来自
``common_message.sender_display_name``，不能 JOIN lark/private 表。
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


USER_ID = "00000000-0000-7000-8000-000000000001"
CHAT_ID = "00000000-0000-7000-8000-000000000002"
MSG_ID = "00000000-0000-7000-8000-000000000003"
ROOT_ID = "00000000-0000-7000-8000-000000000004"


@asynccontextmanager
async def _fake_auto_tx():
    yield


def _patch_session(session):
    return [
        patch("app.data.queries.messages.auto_tx", _fake_auto_tx),
        patch("app.data.queries.messages.current_session", return_value=session),
    ]


class _FakeMsg:
    def __init__(self, *, sender_display_name: str | None):
        self.common_message_id = MSG_ID
        self.common_user_id = USER_ID
        self.sender_display_name = sender_display_name
        self.content = [{"type": "text", "value": "hello"}]
        self.content_text = "hello"
        self.role = "user"
        self.common_root_message_id = ROOT_ID
        self.common_reply_message_id = None
        self.common_conversation_id = CHAT_ID
        self.scope = "group"
        self.event_time = 1000
        self.message_type = "text"
        self.bot_name = None
        self.response_id = None


@pytest.mark.asyncio
async def test_by_root_reads_sender_display_name_no_lark_join():
    """根链查询：发送者名读 common_message.sender_display_name。"""
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_Result([(_FakeMsg(sender_display_name="Alice"), "群A", "p1")])
    )

    patches = _patch_session(session)
    for p in patches:
        p.start()
    try:
        out = await find_messages_with_user_chat_persona_by_root(
            root_message_id=ROOT_ID,
            until_create_time=2000,
        )
        assert len(out) == 1
        msg, username, chat_name, persona_id = out[0]
        assert username == "Alice"
        assert msg.message_id == MSG_ID

        sql_text = str(session.execute.await_args.args[0]).lower()
        assert "common_message" in sql_text
        assert "sender_display_name" in sql_text
        assert "lark_" not in sql_text
        assert "union_id" not in sql_text
        assert "coalesce" not in sql_text
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_in_chat_reads_sender_display_name_no_lark_join():
    """补充窗口查询：无 lark/private JOIN，读 sender_display_name。"""
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_Result([(_FakeMsg(sender_display_name=None), "群B", None)])
    )

    patches = _patch_session(session)
    for p in patches:
        p.start()
    try:
        out = await find_messages_with_user_chat_persona_in_chat(
            chat_id=CHAT_ID,
            exclude_root_message_id=ROOT_ID,
            after_create_time=0,
            before_create_time=9999,
            exclude_user_id="__proactive__",
            limit=10,
        )
        assert len(out) == 1
        msg, username, chat_name, persona_id = out[0]
        assert username is None  # 列为空时如实 None，不退飞书 ID 反查

        sql_text = str(session.execute.await_args.args[0]).lower()
        assert "common_message" in sql_text
        assert "sender_display_name" in sql_text
        assert "lark_" not in sql_text
        assert "union_id" not in sql_text
        assert "coalesce" not in sql_text
    finally:
        for p in patches:
            p.stop()
