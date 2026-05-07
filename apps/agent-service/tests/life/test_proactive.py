"""Proactive emits Message + ChatTrigger via runtime emit.

Invariant: After writing a ConversationMessage, proactive emits two
Data instances in order: ``Message.from_cm(msg)`` first (so memory v4
sees the row), then ``ChatTrigger(...)`` (so route_chat_node fan-outs
into ChatRequest). chat_request mq.publish is gone — emit() is the
single entry.
"""
from __future__ import annotations

import pytest

from app.domain.chat_dataflow import ChatTrigger
from app.domain.message import Message


@pytest.mark.asyncio
async def test_proactive_submit_emits_message_then_chat_trigger(capture_emit, monkeypatch):
    """submit_proactive_chat → DB write → emit(Message) → emit(ChatTrigger)."""
    from app.life import proactive as pro

    # Stub DB session — 我们关心 emit 调用，不关心 DB 落盘。
    class _FakeSession:
        def add(self, _msg):
            pass

    class _FakeSessionCtx:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(pro, "get_session", lambda: _FakeSessionCtx())

    # Stub queries.resolve_bot_name_for_persona (called inside submit_proactive_chat)
    async def fake_resolve_bot_name(*args, **kwargs):
        return "赤尾"

    from app.data import queries as Q

    monkeypatch.setattr(Q, "resolve_bot_name_for_persona", fake_resolve_bot_name)

    # Stub current_lane for ChatTrigger.lane (imported locally inside function)
    import app.infra.rabbitmq
    monkeypatch.setattr(app.infra.rabbitmq, "current_lane", lambda: "prod")

    session_id = await pro.submit_proactive_chat(
        chat_id="c1",
        persona_id="p1",
        target_message_id=None,
        stimulus="hi",
    )

    assert len(capture_emit) == 2, f"expect 2 emits (Message, ChatTrigger), got {capture_emit}"

    msg_emitted = capture_emit[0]
    assert isinstance(msg_emitted, Message)
    assert msg_emitted.chat_id == "c1"
    assert msg_emitted.bot_name == "赤尾"
    assert msg_emitted.message_type == "proactive_trigger"
    assert msg_emitted.role == "user"

    trigger = capture_emit[1]
    assert isinstance(trigger, ChatTrigger)
    assert trigger.chat_id == "c1"
    assert trigger.bot_name == "赤尾"
    assert trigger.is_proactive is True
    assert trigger.user_id == "__proactive__"
    assert trigger.lane == "prod"
    assert trigger.session_id == session_id

    assert session_id is not None
