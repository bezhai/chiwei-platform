"""Tests for app.chat.context trigger-info assembly.

旧的 proactive 外部判断器旁路已删除：``build_chat_context`` 不再检测合成的
``__proactive__`` 触发、不再有 ``is_proactive_var`` / ``proactive_stimulus_var``。
历史 ``proactive_trigger`` 伪消息的排除已下沉到 DB 查询层（quick_search 用的
``find_messages_with_user_chat_persona_*``），见 tests/data/
test_quick_search_proactive_filter.py。这里只锁普通触发的上下文装配。
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.chat.context import build_chat_context
from app.chat.quick_search import QuickSearchResult


def _v2_text(text: str) -> str:
    return json.dumps(
        {
            "v": 2,
            "text": text,
            "items": [{"type": "text", "value": text}],
        },
        ensure_ascii=False,
    )


def _msg(
    message_id: str,
    *,
    text: str,
    user_id: str,
    username: str | None,
    minute: int,
    reply_message_id: str | None = None,
) -> QuickSearchResult:
    return QuickSearchResult(
        message_id=message_id,
        content=_v2_text(text),
        user_id=user_id,
        create_time=datetime(2026, 4, 21, 18, minute, 0),
        role="user",
        username=username,
        chat_type="group",
        chat_name="测试群",
        reply_message_id=reply_message_id,
        chat_id="oc_test",
    )


@pytest.mark.asyncio
async def test_build_chat_context_uses_last_message_as_trigger():
    """普通群聊触发：trigger 信息取最后一条，chain_user_ids 去重保序。"""
    history = [
        _msg(
            "m_target",
            text="我觉得这才是最大的卡点",
            user_id="u_target",
            username="田泽鑫",
            minute=0,
        ),
        _msg(
            "m_current",
            text="@千凪 我要你喂",
            user_id="u_current",
            username="王浩任",
            minute=34,
            reply_message_id="m_target",
        ),
    ]
    build_group_mock = MagicMock(return_value=["built"])

    with (
        patch("app.chat.context.quick_search", new=AsyncMock(return_value=history)),
        patch("app.chat.context.collect_images", new=AsyncMock(return_value=({}, {}))),
        patch("app.chat.context.ImageRegistry", new=MagicMock(return_value="registry")),
        patch("app.chat.context.build_group_messages", new=build_group_mock),
    ):
        ctx = await build_chat_context("m_current")

    assert ctx.messages == ["built"]
    assert ctx.trigger_username == "王浩任"
    assert ctx.trigger_user_id == "u_current"
    assert ctx.chat_name == "测试群"
    assert ctx.chain_user_ids == ["u_target", "u_current"]

    passed_messages, trigger_id, *_ = build_group_mock.call_args.args
    assert [msg.message_id for msg in passed_messages] == ["m_target", "m_current"]
    assert trigger_id == "m_current"


@pytest.mark.asyncio
async def test_build_chat_context_empty_history_returns_empty():
    """quick_search 无结果 -> 空 ChatContext（不抛）。"""
    with (
        patch("app.chat.context.quick_search", new=AsyncMock(return_value=[])),
    ):
        ctx = await build_chat_context("m_missing")

    assert ctx.messages == []
    assert ctx.trigger_username == ""
    assert ctx.trigger_user_id == ""
    assert ctx.chain_user_ids == []
