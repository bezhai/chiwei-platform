"""Tests for app.chat.context proactive trigger handling."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.chat.context import (
    PROACTIVE_USER_ID,
    build_chat_context,
    is_proactive_var,
    proactive_stimulus_var,
)
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
async def test_build_chat_context_ignores_historical_proactive_for_normal_trigger():
    history = [
        _msg(
            "m_target",
            text="我觉得这才是最大的卡点",
            user_id="u_target",
            username="田泽鑫",
            minute=0,
        ),
        _msg(
            "pro_old",
            text="聊一下 AI 写需求后的测试思路",
            user_id=PROACTIVE_USER_ID,
            username=None,
            minute=10,
            reply_message_id="m_target",
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
        patch("app.chat.context._collect_images", new=AsyncMock(return_value=({}, {}))),
        patch("app.chat.context.get_redis", new=AsyncMock(return_value=MagicMock())),
        patch("app.chat.context.ImageRegistry", new=MagicMock(return_value="registry")),
        patch("app.chat.context._build_group_messages", new=build_group_mock),
    ):
        ctx = await build_chat_context("m_current")

    assert ctx.messages == ["built"]
    assert ctx.trigger_username == "王浩任"
    assert ctx.trigger_user_id == "u_current"
    assert ctx.chat_name == "测试群"
    assert ctx.chain_user_ids == ["u_target", "u_current"]
    assert is_proactive_var.get(False) is False
    assert proactive_stimulus_var.get("") == ""

    passed_messages, trigger_id, *_ = build_group_mock.call_args.args
    assert [msg.message_id for msg in passed_messages] == ["m_target", "m_current"]
    assert trigger_id == "m_current"


@pytest.mark.asyncio
async def test_build_chat_context_uses_current_proactive_trigger_only():
    history = [
        _msg(
            "m_target",
            text="我觉得这才是最大的卡点",
            user_id="u_target",
            username="田泽鑫",
            minute=0,
        ),
        _msg(
            "pro_old",
            text="旧 proactive",
            user_id=PROACTIVE_USER_ID,
            username=None,
            minute=5,
            reply_message_id="m_old_target",
        ),
        _msg(
            "m_recent",
            text="@千凪 那我们吃啥",
            user_id="u_recent",
            username="冯宇林",
            minute=22,
            reply_message_id="m_target",
        ),
        _msg(
            "pro_now",
            text="聊一下 AI 写需求后的测试思路",
            user_id=PROACTIVE_USER_ID,
            username=None,
            minute=34,
            reply_message_id="m_target",
        ),
    ]
    build_group_mock = MagicMock(return_value=["built"])

    with (
        patch("app.chat.context.quick_search", new=AsyncMock(return_value=history)),
        patch("app.chat.context._collect_images", new=AsyncMock(return_value=({}, {}))),
        patch("app.chat.context.get_redis", new=AsyncMock(return_value=MagicMock())),
        patch("app.chat.context.ImageRegistry", new=MagicMock(return_value="registry")),
        patch("app.chat.context._build_group_messages", new=build_group_mock),
    ):
        ctx = await build_chat_context("pro_now")

    assert ctx.messages == ["built"]
    assert ctx.trigger_username == ""
    assert ctx.trigger_user_id == ""
    assert ctx.chat_name == "测试群"
    assert ctx.chain_user_ids == ["u_target", "u_recent"]
    assert is_proactive_var.get(False) is True
    assert proactive_stimulus_var.get("") == "聊一下 AI 写需求后的测试思路"

    passed_messages, trigger_id, *_ = build_group_mock.call_args.args
    assert [msg.message_id for msg in passed_messages] == ["m_target", "m_recent"]
    assert trigger_id == "m_target"
