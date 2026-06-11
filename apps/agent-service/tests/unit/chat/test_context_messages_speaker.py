"""Tests for app.chat._context_messages group speaker rendering.

身份全局化后删了 lark_user JOIN：assistant 行本就无 username，
历史 user 行迁移前也全空。group 上下文不能对所有 role 都
`username or 占位`——否则机器人（赤尾）历史发言会被渲染成占位词喂给
模型，误导上下文。assistant 行必须按 role 派生固定说话人（用 "我"），
只有 user 行才 `username or 占位`。
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

from app.chat._context_messages import build_group_messages
from app.chat.quick_search import QuickSearchResult


def _v2_text(text: str) -> str:
    return json.dumps(
        {"v": 2, "text": text, "items": [{"type": "text", "value": text}]},
        ensure_ascii=False,
    )


def _msg(
    message_id: str,
    *,
    text: str,
    role: str,
    username: str | None,
    minute: int,
    reply_message_id: str | None = None,
) -> QuickSearchResult:
    return QuickSearchResult(
        message_id=message_id,
        content=_v2_text(text),
        user_id="u",
        create_time=datetime(2026, 4, 21, 18, minute, 0),
        role=role,
        username=username,
        chat_type="group",
        chat_name="测试群",
        reply_message_id=reply_message_id,
        chat_id="oc_test",
    )


def _run(messages, trigger_id):
    """Render group messages with get_prompt stubbed to echo its kwargs."""
    fake_prompt = MagicMock()
    fake_prompt.compile.side_effect = (
        lambda *, reply_chain, other_messages: (
            f"REPLY_CHAIN:\n{reply_chain}\nOTHER:\n{other_messages}"
        )
    )
    with patch(
        "app.chat._context_messages.get_prompt",
        return_value=fake_prompt,
    ):
        out = build_group_messages(messages, trigger_id, {}, {})
    # single neutral USER Message with a text content block
    return out[0].content[0].text


def test_assistant_row_not_rendered_as_placeholder_in_reply_chain():
    """赤尾历史发言（assistant，username 空）在回复链里不能显示成占位词。"""
    history = [
        _msg("m_user", text="在吗", role="user", username="田泽鑫", minute=0),
        _msg(
            "m_bot",
            text="在的",
            role="assistant",
            username=None,
            minute=1,
            reply_message_id="m_user",
        ),
    ]
    rendered = _run(history, "m_bot")

    assert "未知用户" not in rendered
    assert "我: 在的" in rendered
    assert "田泽鑫: 在吗" in rendered


def test_assistant_row_not_rendered_as_placeholder_in_other_messages():
    """非回复链的 assistant 历史行同样不能显示成占位词。"""
    history = [
        _msg("m_a", text="A 说话", role="user", username="王浩任", minute=0),
        _msg("m_bot", text="赤尾插话", role="assistant", username=None, minute=1),
        _msg(
            "m_trigger",
            text="@千凪 你怎么看",
            role="user",
            username="冯宇林",
            minute=2,
        ),
    ]
    rendered = _run(history, "m_trigger")

    assert "未知用户" not in rendered
    assert "我: 赤尾插话" in rendered


def test_user_row_without_username_still_uses_placeholder():
    """user 行（迁移前 username 为空）保持占位语义，不被误判成 assistant。"""
    history = [
        _msg("m_x", text="老消息没名字", role="user", username=None, minute=0),
        _msg(
            "m_trigger",
            text="@千凪 在吗",
            role="user",
            username="王浩任",
            minute=1,
        ),
    ]
    rendered = _run(history, "m_trigger")

    assert "未知用户: 老消息没名字" in rendered
    assert "王浩任: @千凪 在吗" in rendered
