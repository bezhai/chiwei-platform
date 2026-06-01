"""Tests for app.life.proactive on the common channel layer."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from app.data.message_record import CommonMessageRecord

MODULE = "app.life.proactive"

SESSION_ID = "00000000-0000-7000-8000-000000000001"
MESSAGE_ID = "00000000-0000-7000-8000-000000000002"
CHAT_ID = "00000000-0000-7000-8000-000000000003"
TARGET_ID = "00000000-0000-7000-8000-000000000004"
ROOT_ID = "00000000-0000-7000-8000-000000000005"
OTHER_CHAT_ID = "00000000-0000-7000-8000-000000000006"
USER_ID = "00000000-0000-7000-8000-000000000007"


@asynccontextmanager
async def _fake_tx():
    yield


def _make_emit_tx_mock():
    captured: list = []

    async def _fake_emit_tx(ev):
        captured.append(ev)

    return _fake_emit_tx, captured


def _make_insert_mock():
    captured: list = []

    async def _fake_insert(message):
        captured.append(message)

    return _fake_insert, captured


def _target_record(*, chat_id: str = CHAT_ID) -> CommonMessageRecord:
    return CommonMessageRecord(
        message_id=TARGET_ID,
        user_id=USER_ID,
        username="Alice",
        content='{"v":2,"text":"target","items":[]}',
        role="user",
        root_message_id=ROOT_ID,
        reply_message_id=None,
        chat_id=chat_id,
        chat_type="group",
        create_time=1000,
        message_type="text",
        bot_name=None,
        response_id=None,
    )


@pytest.mark.asyncio
async def test_submit_proactive_chat_writes_common_message_and_emits_request_then_trigger():
    from app.domain.chat_dataflow import ChatTrigger
    from app.domain.message_request import MessageRequest
    from app.life.proactive import submit_proactive_chat

    fake_emit, captured = _make_emit_tx_mock()
    fake_insert, inserted = _make_insert_mock()

    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch("app.data.queries.insert_proactive_message", fake_insert),
        patch(
            "app.data.queries.resolve_bot_name_for_persona",
            AsyncMock(return_value="akao"),
        ),
        patch(f"{MODULE}.time.time", return_value=1234.567),
        patch(
            f"{MODULE}.uuid7",
            side_effect=[UUID(SESSION_ID), UUID(MESSAGE_ID)],
        ),
        patch(f"{MODULE}.emit_tx", fake_emit),
        patch("app.infra.rabbitmq.current_lane", return_value="prod"),
    ):
        session_id = await submit_proactive_chat(
            chat_id=CHAT_ID,
            persona_id="akao-001",
            target_message_id=None,
            stimulus="想接一句",
        )

    assert session_id == SESSION_ID
    assert len(inserted) == 1
    added = inserted[0]
    assert str(added.common_message_id) == MESSAGE_ID
    assert added.channel == "system"
    assert str(added.common_conversation_id) == CHAT_ID
    assert added.common_user_id is None
    assert added.sender_display_name == "proactive"
    assert str(added.common_root_message_id) == MESSAGE_ID
    assert added.common_reply_message_id is None
    assert added.message_type == "proactive_trigger"
    assert added.response_id == SESSION_ID
    assert added.content == [{"type": "text", "value": "想接一句"}]

    assert len(captured) == 2
    assert isinstance(captured[0], MessageRequest)
    assert captured[0].message_id == MESSAGE_ID

    trigger = captured[1]
    assert isinstance(trigger, ChatTrigger)
    assert trigger.message_id == MESSAGE_ID
    assert trigger.session_id == SESSION_ID
    assert trigger.chat_id == CHAT_ID
    assert trigger.root_id is None
    assert trigger.user_id == "__proactive__"
    assert trigger.bot_name == "akao"
    assert trigger.is_proactive is True
    assert trigger.lane == "prod"


@pytest.mark.asyncio
async def test_submit_proactive_chat_uses_same_chat_common_target():
    from app.domain.chat_dataflow import ChatTrigger
    from app.life.proactive import submit_proactive_chat

    fake_emit, captured = _make_emit_tx_mock()
    fake_insert, inserted = _make_insert_mock()
    mock_find = AsyncMock(return_value=_target_record())

    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch("app.data.queries.insert_proactive_message", fake_insert),
        patch("app.data.queries.find_message_by_id", mock_find),
        patch(
            "app.data.queries.resolve_bot_name_for_persona",
            AsyncMock(return_value="akao"),
        ),
        patch(f"{MODULE}.time.time", return_value=1234.567),
        patch(
            f"{MODULE}.uuid7",
            side_effect=[UUID(SESSION_ID), UUID(MESSAGE_ID)],
        ),
        patch(f"{MODULE}.emit_tx", fake_emit),
        patch("app.infra.rabbitmq.current_lane", return_value="prod"),
    ):
        await submit_proactive_chat(
            chat_id=CHAT_ID,
            persona_id="akao-001",
            target_message_id=TARGET_ID,
            stimulus="想接一句",
        )

    mock_find.assert_awaited_once_with(TARGET_ID)
    assert len(inserted) == 1
    added = inserted[0]
    assert str(added.common_root_message_id) == ROOT_ID
    assert str(added.common_reply_message_id) == TARGET_ID

    trigger = next((d for d in captured if isinstance(d, ChatTrigger)), None)
    assert trigger is not None
    assert trigger.root_id == TARGET_ID


@pytest.mark.asyncio
async def test_submit_proactive_chat_ignores_target_from_other_common_chat():
    from app.domain.chat_dataflow import ChatTrigger
    from app.life.proactive import submit_proactive_chat

    fake_emit, captured = _make_emit_tx_mock()
    fake_insert, inserted = _make_insert_mock()

    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch("app.data.queries.insert_proactive_message", fake_insert),
        patch(
            "app.data.queries.find_message_by_id",
            AsyncMock(return_value=_target_record(chat_id=OTHER_CHAT_ID)),
        ),
        patch(
            "app.data.queries.resolve_bot_name_for_persona",
            AsyncMock(return_value="akao"),
        ),
        patch(f"{MODULE}.time.time", return_value=1234.567),
        patch(
            f"{MODULE}.uuid7",
            side_effect=[UUID(SESSION_ID), UUID(MESSAGE_ID)],
        ),
        patch(f"{MODULE}.emit_tx", fake_emit),
        patch("app.infra.rabbitmq.current_lane", return_value="prod"),
    ):
        await submit_proactive_chat(
            chat_id=CHAT_ID,
            persona_id="akao-001",
            target_message_id=TARGET_ID,
            stimulus="想接一句",
        )

    assert len(inserted) == 1
    added = inserted[0]
    assert str(added.common_root_message_id) == MESSAGE_ID
    assert added.common_reply_message_id is None

    trigger = next((d for d in captured if isinstance(d, ChatTrigger)), None)
    assert trigger is not None
    assert trigger.root_id is None
