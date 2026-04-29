"""Tests for chat/post_actions.py."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.domain.safety import PostSafetyRequest


@pytest.mark.asyncio
async def test_publish_post_check_emits_post_safety_request():
    """_publish_post_check emits PostSafetyRequest into the dataflow graph."""
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


@pytest.mark.asyncio
async def test_emit_memory_trigger_swallows_exception(monkeypatch, caplog):
    """fire-and-forget 语义：emit 失败被吞 + log error，不冒泡。"""
    import logging

    caplog.set_level(logging.ERROR, logger="app.chat.post_actions")

    from app.chat.post_actions import _emit_memory_trigger
    from app.domain.memory_triggers import DriftTrigger
    from unittest.mock import AsyncMock

    fake_emit = AsyncMock(side_effect=RuntimeError("redis down"))
    monkeypatch.setattr("app.chat.post_actions.emit", fake_emit)

    # 不应该 raise
    await _emit_memory_trigger(DriftTrigger(chat_id="c1", persona_id="p1"))

    # 异常被 logger.exception 吃掉
    assert any("failed to emit memory trigger" in r.message
               for r in caplog.records)


@pytest.mark.asyncio
async def test_emit_memory_trigger_calls_emit_on_success(monkeypatch):
    from app.chat.post_actions import _emit_memory_trigger
    from app.domain.memory_triggers import DriftTrigger
    from unittest.mock import AsyncMock

    fake_emit = AsyncMock(return_value=None)
    monkeypatch.setattr("app.chat.post_actions.emit", fake_emit)

    t = DriftTrigger(chat_id="c1", persona_id="p1")
    await _emit_memory_trigger(t)

    fake_emit.assert_awaited_once_with(t)
