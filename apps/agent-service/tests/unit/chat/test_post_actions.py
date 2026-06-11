"""Tests for chat/post_actions.py.

v4 afterthought 触发链已删除：schedule_post_actions 只剩 post safety 一个
fire-and-forget 动作（persona_id 参数随 afterthought 一起拆除）。
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

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
            channel="qq",
            response_text="hello",
            chat_id="chat-1",
            trigger_message_id="msg-1",
        )

    assert len(captured) == 1
    req = captured[0]
    assert isinstance(req, PostSafetyRequest)
    assert req.session_id == "sess-1"
    assert req.channel == "qq"
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
            channel="qq",
            response_text="hello",
            chat_id="chat-1",
            trigger_message_id="msg-1",
        )


@pytest.mark.asyncio
async def test_schedule_post_actions_emits_only_post_safety(monkeypatch):
    """v4 afterthought 已删：有 session_id 时只 emit PostSafetyRequest，
    不再有任何 memory trigger。"""
    from app.chat import post_actions

    fake_emit = AsyncMock(return_value=None)
    monkeypatch.setattr("app.chat.post_actions.emit", fake_emit)

    await post_actions.schedule_post_actions(
        full_content="hello",
        session_id="sess-1",
        channel="lark",
        chat_id="c1",
        message_id="m1",
    )

    emitted = [type(c.args[0]).__name__ for c in fake_emit.await_args_list]
    assert emitted == ["PostSafetyRequest"]


@pytest.mark.asyncio
async def test_schedule_post_actions_no_emit_without_session(monkeypatch):
    """session_id=None → post safety 跳过；afterthought 已删 → 一次 emit 都没有。"""
    from app.chat import post_actions

    fake_emit = AsyncMock(return_value=None)
    monkeypatch.setattr("app.chat.post_actions.emit", fake_emit)

    await post_actions.schedule_post_actions(
        full_content="hello",
        session_id=None,
        channel="lark",
        chat_id="c1",
        message_id="m1",
    )

    fake_emit.assert_not_awaited()


def test_post_actions_has_no_memory_trigger_surface():
    """afterthought 拆除后 post_actions 不残留 memory trigger 痕迹：
    没有 _emit_memory_trigger helper，签名里没有 persona_id。"""
    from app.chat import post_actions

    assert not hasattr(post_actions, "_emit_memory_trigger")
    params = inspect.signature(post_actions.schedule_post_actions).parameters
    assert "persona_id" not in params
