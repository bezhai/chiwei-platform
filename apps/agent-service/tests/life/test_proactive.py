"""Proactive appends MessageRequest + ChatTrigger to outbox via emit_tx.

Invariant: After writing a CommonMessage, proactive appends
``MessageRequest(message_id=...)`` first (so vectorize hydrates common_message),
then ``ChatTrigger(...)`` (so dispatcher fires route_chat_node fan-outs into
ChatRequest).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.domain.chat_dataflow import ChatTrigger
from app.domain.message_request import MessageRequest

CHAT_ID = "00000000-0000-7000-8000-000000000003"


@pytest.mark.asyncio
async def test_proactive_submit_emits_message_then_chat_trigger(monkeypatch):
    """submit_proactive_chat -> DB write -> MessageRequest -> ChatTrigger."""
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

    # emit_tx is imported at the proactive module top level; patch it
    # there so the test substitution wins over the live runtime.db ref.
    monkeypatch.setattr(pro, "emit_tx", _fake_emit_tx)

    # Stub queries.resolve_bot_name_for_persona
    async def fake_resolve_bot_name(*args, **kwargs):
        return "赤尾"

    monkeypatch.setattr(Q, "resolve_bot_name_for_persona", fake_resolve_bot_name)

    # Stub current_lane for ChatTrigger.lane
    import app.infra.rabbitmq
    monkeypatch.setattr(app.infra.rabbitmq, "current_lane", lambda: "prod")

    session_id = await pro.submit_proactive_chat(
        chat_id=CHAT_ID,
        persona_id="p1",
        target_message_id=None,
        stimulus="hi",
    )

    assert len(inserted) == 1, f"expect 1 insert_proactive_message call, got {inserted}"
    assert len(captured) == 2, (
        f"expect 2 appends (MessageRequest, ChatTrigger), got {captured}"
    )

    msg_emitted = captured[0]
    assert isinstance(msg_emitted, MessageRequest)

    trigger = captured[1]
    assert isinstance(trigger, ChatTrigger)
    assert trigger.message_id == msg_emitted.message_id
    assert trigger.chat_id == CHAT_ID
    assert trigger.bot_name == "赤尾"
    assert trigger.is_proactive is True
    assert trigger.user_id == "__proactive__"
    assert trigger.lane == "prod"
    assert trigger.session_id == session_id

    assert session_id is not None
