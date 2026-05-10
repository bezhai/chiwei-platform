"""Tests for app.life.proactive."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

MODULE = "app.life.proactive"


@asynccontextmanager
async def _fake_tx():
    yield


def _make_emit_tx_mock():
    captured: list = []

    async def _fake_emit_tx(ev):
        captured.append(ev)

    return _fake_emit_tx, captured


def _make_insert_mock():
    """Capture ConversationMessage entities passed to insert_proactive_message."""
    captured: list = []

    async def _fake_insert(message):
        captured.append(message)

    return _fake_insert, captured


@pytest.mark.asyncio
async def test_submit_proactive_chat_uses_existing_lark_target_root():
    from app.domain.chat_dataflow import ChatTrigger
    from app.domain.message import Message
    from app.life.proactive import submit_proactive_chat

    target = SimpleNamespace(
        message_id="om_target",
        root_message_id="om_root",
        chat_id="oc_test",
    )
    fake_emit, captured = _make_emit_tx_mock()
    fake_insert, inserted = _make_insert_mock()

    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch("app.data.queries.insert_proactive_message", fake_insert),
        patch("app.data.queries.find_message_by_id", AsyncMock(return_value=target)),
        patch(
            "app.data.queries.resolve_bot_name_for_persona",
            AsyncMock(return_value="akao"),
        ),
        patch(f"{MODULE}.time.time", return_value=1234.567),
        patch(f"{MODULE}.uuid.uuid4", return_value="session-1"),
        # emit_tx is imported lazily inside submit_proactive_chat to avoid boot
        # cycles, so patch the underlying source rather than MODULE.emit_tx.
        patch("app.runtime.db.emit_tx", fake_emit),
        patch("app.infra.rabbitmq.current_lane", return_value="prod"),
    ):
        session_id = await submit_proactive_chat(
            chat_id="oc_test",
            persona_id="akao-001",
            target_message_id="om_target",
            stimulus="想接一句",
        )

    assert session_id == "session-1"
    assert len(inserted) == 1, f"expect 1 insert_proactive_message call, got {inserted}"
    added = inserted[0]
    assert added.message_id == "proactive_1234567"
    assert added.root_message_id == "om_root"
    assert added.reply_message_id == "om_target"

    # Both Message and ChatTrigger are appended to the outbox in call order
    assert len(captured) == 2, f"expect 2 appends, got {len(captured)}: {captured}"
    assert isinstance(captured[0], Message), (
        f"first append must be Message, got {type(captured[0]).__name__}"
    )
    assert isinstance(captured[1], ChatTrigger), (
        f"second append must be ChatTrigger, got {type(captured[1]).__name__}"
    )
    trigger = captured[1]
    assert trigger.message_id == "proactive_1234567"
    assert trigger.session_id == "session-1"
    assert trigger.chat_id == "oc_test"
    assert trigger.is_p2p is False
    assert trigger.root_id == "om_target"
    assert trigger.user_id == "__proactive__"
    assert trigger.bot_name == "akao"
    assert trigger.is_proactive is True
    assert trigger.lane == "prod"


@pytest.mark.asyncio
async def test_submit_proactive_chat_resolves_numeric_target_row_id():
    from app.domain.chat_dataflow import ChatTrigger
    from app.life.proactive import submit_proactive_chat

    target = SimpleNamespace(
        message_id="om_from_row",
        root_message_id="om_root",
        chat_id="oc_test",
    )
    fake_emit, captured = _make_emit_tx_mock()
    fake_insert, inserted = _make_insert_mock()

    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch("app.data.queries.insert_proactive_message", fake_insert),
        patch(
            "app.data.queries.resolve_message_id_by_row_id",
            AsyncMock(return_value="om_from_row"),
        ) as mock_resolve_row,
        patch("app.data.queries.find_message_by_id", AsyncMock(return_value=target)),
        patch(
            "app.data.queries.resolve_bot_name_for_persona",
            AsyncMock(return_value="akao"),
        ),
        patch(f"{MODULE}.time.time", return_value=1234.567),
        patch(f"{MODULE}.uuid.uuid4", return_value="session-2"),
        patch("app.runtime.db.emit_tx", fake_emit),
        patch("app.infra.rabbitmq.current_lane", return_value="prod"),
    ):
        await submit_proactive_chat(
            chat_id="oc_test",
            persona_id="akao-001",
            target_message_id="42",
            stimulus="想接一句",
        )

    mock_resolve_row.assert_awaited_once()
    assert len(inserted) == 1
    added = inserted[0]
    assert added.root_message_id == "om_root"
    assert added.reply_message_id == "om_from_row"

    trigger = next((d for d in captured if isinstance(d, ChatTrigger)), None)
    assert trigger is not None, f"no ChatTrigger in captured: {captured}"
    assert trigger.root_id == "om_from_row"


@pytest.mark.asyncio
async def test_submit_proactive_chat_ignores_target_from_other_chat():
    from app.domain.chat_dataflow import ChatTrigger
    from app.life.proactive import submit_proactive_chat

    target = SimpleNamespace(
        message_id="om_other",
        root_message_id="om_other_root",
        chat_id="oc_other",
    )
    fake_emit, captured = _make_emit_tx_mock()
    fake_insert, inserted = _make_insert_mock()

    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch("app.data.queries.insert_proactive_message", fake_insert),
        patch("app.data.queries.find_message_by_id", AsyncMock(return_value=target)),
        patch(
            "app.data.queries.resolve_bot_name_for_persona",
            AsyncMock(return_value="akao"),
        ),
        patch(f"{MODULE}.time.time", return_value=1234.567),
        patch(f"{MODULE}.uuid.uuid4", return_value="session-3"),
        patch("app.runtime.db.emit_tx", fake_emit),
        patch("app.infra.rabbitmq.current_lane", return_value="prod"),
    ):
        await submit_proactive_chat(
            chat_id="oc_test",
            persona_id="akao-001",
            target_message_id="om_other",
            stimulus="想接一句",
        )

    assert len(inserted) == 1
    added = inserted[0]
    assert added.root_message_id == "proactive_1234567"
    assert added.reply_message_id is None

    # Cross-chat target is ignored → ChatTrigger.root_id should be None
    trigger = next((d for d in captured if isinstance(d, ChatTrigger)), None)
    assert trigger is not None, f"no ChatTrigger in captured: {captured}"
    assert trigger.root_id is None
