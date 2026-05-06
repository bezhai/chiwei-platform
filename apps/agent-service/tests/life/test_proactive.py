"""Phase 5b — proactive 写完 ConversationMessage 后直接 emit Message（不经 Bridge）。

invariant 测试：5b 之前 proactive 经 ``emit_legacy_message(msg)``，
5b 之后直连 ``await emit(Message.from_cm(msg))``，对 capture_emit
fixture 的可观察行为相同——一条 ``Message`` 被 emit。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.domain.message import Message


@pytest.mark.asyncio
async def test_proactive_submit_emits_message_directly(capture_emit, monkeypatch):
    """submit_proactive_chat → DB write → emit(Message)."""
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

    # Stub mq.publish — 不关心 RabbitMQ 实际发布。
    fake_publish = AsyncMock()
    monkeypatch.setattr(pro.mq, "publish", fake_publish)

    # Stub current_lane for chat_request publish (imported locally inside function)
    import app.infra.rabbitmq
    monkeypatch.setattr(app.infra.rabbitmq, "current_lane", lambda: "prod")

    session_id = await pro.submit_proactive_chat(
        chat_id="c1",
        persona_id="p1",
        target_message_id=None,
        stimulus="hi",
    )

    # Proactive should have called emit once with a Message instance.
    assert len(capture_emit) == 1
    emitted = capture_emit[0]
    assert isinstance(emitted, Message)
    assert emitted.chat_id == "c1"
    assert emitted.bot_name == "赤尾"
    assert emitted.message_type == "proactive_trigger"
    assert emitted.role == "user"
    assert session_id is not None
