"""Proactive appends Message + ChatTrigger to outbox via emit_tx.

Invariant: After writing a ConversationMessage, proactive appends two
Data instances in order: ``Message.from_cm(msg)`` first (so memory v4
sees the row), then ``ChatTrigger(...)`` (so dispatcher fires
route_chat_node fan-outs into ChatRequest).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.domain.chat_dataflow import ChatTrigger
from app.domain.message import Message


@pytest.mark.asyncio
async def test_proactive_submit_emits_message_then_chat_trigger(monkeypatch):
    """submit_proactive_chat → DB write → outbox(Message) → outbox(ChatTrigger)."""
    from app.life import proactive as pro

    captured: list = []

    async def _fake_emit_tx(ev):
        captured.append(ev)

    @asynccontextmanager
    async def _fake_tx():
        yield

    # Stub DB session — we care about outbox appends, not DB commit.
    class _FakeSession:
        def add(self, _msg):
            pass

    monkeypatch.setattr(pro, "tx", _fake_tx)
    monkeypatch.setattr(pro, "current_session", lambda: _FakeSession())

    # Patch emit_tx at the runtime.db module level (local import inside function)
    import app.runtime.db
    monkeypatch.setattr(app.runtime.db, "emit_tx", _fake_emit_tx)

    # Stub queries.resolve_bot_name_for_persona
    async def fake_resolve_bot_name(*args, **kwargs):
        return "赤尾"

    from app.data import queries as Q
    monkeypatch.setattr(Q, "resolve_bot_name_for_persona", fake_resolve_bot_name)

    # Stub current_lane for ChatTrigger.lane
    import app.infra.rabbitmq
    monkeypatch.setattr(app.infra.rabbitmq, "current_lane", lambda: "prod")

    session_id = await pro.submit_proactive_chat(
        chat_id="c1",
        persona_id="p1",
        target_message_id=None,
        stimulus="hi",
    )

    assert len(captured) == 2, f"expect 2 appends (Message, ChatTrigger), got {captured}"

    msg_emitted = captured[0]
    assert isinstance(msg_emitted, Message)
    assert msg_emitted.chat_id == "c1"
    assert msg_emitted.bot_name == "赤尾"
    assert msg_emitted.message_type == "proactive_trigger"
    assert msg_emitted.role == "user"

    trigger = captured[1]
    assert isinstance(trigger, ChatTrigger)
    assert trigger.chat_id == "c1"
    assert trigger.bot_name == "赤尾"
    assert trigger.is_proactive is True
    assert trigger.user_id == "__proactive__"
    assert trigger.lane == "prod"
    assert trigger.session_id == session_id

    assert session_id is not None
