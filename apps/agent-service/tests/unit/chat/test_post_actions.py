"""Tests for chat/post_actions.py — Phase 2 emit(PostSafetyRequest)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.domain.safety import PostSafetyRequest


@pytest.mark.asyncio
async def test_publish_post_check_emits_post_safety_request():
    """旧 mq.publish(SAFETY_CHECK,...) 替换为 emit(PostSafetyRequest(...))."""
    from app.chat.post_actions import _publish_post_check

    captured: list[PostSafetyRequest] = []

    async def fake_emit(data):
        captured.append(data)

    with patch("app.chat.post_actions.emit", fake_emit):
        await _publish_post_check(
            session_id="sess-1",
            response_text="hello",
            chat_id="chat-1",
            trigger_message_id="msg-1",
        )

    assert len(captured) == 1
    req = captured[0]
    assert isinstance(req, PostSafetyRequest)
    assert req.session_id == "sess-1"
    assert req.trigger_message_id == "msg-1"
    assert req.chat_id == "chat-1"
    assert req.response_text == "hello"


@pytest.mark.asyncio
async def test_publish_post_check_swallows_emit_errors():
    """emit 抛异常不应炸 chat pipeline（fire-and-forget 语义保留）."""
    from app.chat.post_actions import _publish_post_check

    async def fake_emit(data):
        raise RuntimeError("mq down")

    with patch("app.chat.post_actions.emit", fake_emit):
        # 不应抛
        await _publish_post_check(
            session_id="sess-1",
            response_text="hello",
            chat_id="chat-1",
            trigger_message_id="msg-1",
        )
