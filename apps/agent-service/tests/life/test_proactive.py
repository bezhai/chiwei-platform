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
    inserted: list = []

    async def _fake_emit_tx(ev):
        captured.append(ev)

    async def _fake_insert(message):
        inserted.append(message)

    @asynccontextmanager
    async def _fake_tx():
        yield

    monkeypatch.setattr(pro, "tx", _fake_tx)

    # The DB write goes through Q.insert_proactive_message — stub it so
    # we don't open a real session.
    from app.data import queries as Q
    monkeypatch.setattr(Q, "insert_proactive_message", _fake_insert)

    # Patch emit_tx at the runtime.db module level (local import inside function)
    import app.runtime.db
    monkeypatch.setattr(app.runtime.db, "emit_tx", _fake_emit_tx)

    # Stub queries.resolve_bot_name_for_persona
    async def fake_resolve_bot_name(*args, **kwargs):
        return "赤尾"

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

    assert len(inserted) == 1, f"expect 1 insert_proactive_message call, got {inserted}"
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
