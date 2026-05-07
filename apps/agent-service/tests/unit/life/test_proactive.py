"""Tests for app.life.proactive."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "app.life.proactive"


def _mock_session_cm(session):
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _emit_args(mock_emit):
    """Return the list of emitted Data instances in call order."""
    return [call.args[0] for call in mock_emit.await_args_list]


@pytest.mark.asyncio
async def test_submit_proactive_chat_uses_existing_lark_target_root():
    from app.domain.chat_dataflow import ChatTrigger
    from app.domain.message import Message
    from app.life.proactive import submit_proactive_chat

    session = MagicMock()
    target = SimpleNamespace(
        message_id="om_target",
        root_message_id="om_root",
        chat_id="oc_test",
    )

    with (
        patch(f"{MODULE}.get_session", return_value=_mock_session_cm(session)),
        patch("app.data.queries.find_message_by_id", AsyncMock(return_value=target)),
        patch(
            "app.data.queries.resolve_bot_name_for_persona",
            AsyncMock(return_value="akao"),
        ),
        patch(f"{MODULE}.time.time", return_value=1234.567),
        patch(f"{MODULE}.uuid.uuid4", return_value="session-1"),
        patch("app.runtime.emit", AsyncMock()) as mock_emit,
        patch("app.infra.rabbitmq.current_lane", return_value="prod"),
    ):
        session_id = await submit_proactive_chat(
            chat_id="oc_test",
            persona_id="akao-001",
            target_message_id="om_target",
            stimulus="想接一句",
        )

    assert session_id == "session-1"
    added = session.add.call_args.args[0]
    assert added.message_id == "proactive_1234567"
    assert added.root_message_id == "om_root"
    assert added.reply_message_id == "om_target"

    # emit order: Message first (synthetic CM), then ChatTrigger
    args = _emit_args(mock_emit)
    assert len(args) == 2, f"expect 2 emits, got {len(args)}: {args}"
    assert isinstance(args[0], Message), (
        f"first emit must be Message, got {type(args[0]).__name__}"
    )
    assert isinstance(args[1], ChatTrigger), (
        f"second emit must be ChatTrigger, got {type(args[1]).__name__}"
    )
    trigger = args[1]
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

    session = MagicMock()
    target = SimpleNamespace(
        message_id="om_from_row",
        root_message_id="om_root",
        chat_id="oc_test",
    )

    with (
        patch(f"{MODULE}.get_session", return_value=_mock_session_cm(session)),
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
        patch("app.runtime.emit", AsyncMock()) as mock_emit,
        patch("app.infra.rabbitmq.current_lane", return_value="prod"),
    ):
        await submit_proactive_chat(
            chat_id="oc_test",
            persona_id="akao-001",
            target_message_id="42",
            stimulus="想接一句",
        )

    mock_resolve_row.assert_awaited_once()
    added = session.add.call_args.args[0]
    assert added.root_message_id == "om_root"
    assert added.reply_message_id == "om_from_row"

    args = _emit_args(mock_emit)
    trigger = next((d for d in args if isinstance(d, ChatTrigger)), None)
    assert trigger is not None, f"no ChatTrigger in emits: {args}"
    assert trigger.root_id == "om_from_row"


@pytest.mark.asyncio
async def test_submit_proactive_chat_ignores_target_from_other_chat():
    from app.domain.chat_dataflow import ChatTrigger
    from app.life.proactive import submit_proactive_chat

    session = MagicMock()
    target = SimpleNamespace(
        message_id="om_other",
        root_message_id="om_other_root",
        chat_id="oc_other",
    )

    with (
        patch(f"{MODULE}.get_session", return_value=_mock_session_cm(session)),
        patch("app.data.queries.find_message_by_id", AsyncMock(return_value=target)),
        patch(
            "app.data.queries.resolve_bot_name_for_persona",
            AsyncMock(return_value="akao"),
        ),
        patch(f"{MODULE}.time.time", return_value=1234.567),
        patch(f"{MODULE}.uuid.uuid4", return_value="session-3"),
        patch("app.runtime.emit", AsyncMock()) as mock_emit,
        patch("app.infra.rabbitmq.current_lane", return_value="prod"),
    ):
        await submit_proactive_chat(
            chat_id="oc_test",
            persona_id="akao-001",
            target_message_id="om_other",
            stimulus="想接一句",
        )

    added = session.add.call_args.args[0]
    assert added.root_message_id == "proactive_1234567"
    assert added.reply_message_id is None

    # 跨 chat 的 target 被忽略 → ChatTrigger.root_id 应为 None（与 ChatTrigger
    # 字段类型 str | None 一致；不再用空字串作为 sentinel）
    args = _emit_args(mock_emit)
    trigger = next((d for d in args if isinstance(d, ChatTrigger)), None)
    assert trigger is not None, f"no ChatTrigger in emits: {args}"
    assert trigger.root_id is None
