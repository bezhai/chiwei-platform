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


@pytest.mark.asyncio
async def test_submit_proactive_chat_uses_existing_lark_target_root():
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
        patch(f"{MODULE}.mq.publish", AsyncMock()) as mock_publish,
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
    payload = mock_publish.await_args.args[1]
    assert payload["message_id"] == "proactive_1234567"
    assert payload["root_id"] == "om_target"


@pytest.mark.asyncio
async def test_submit_proactive_chat_resolves_numeric_target_row_id():
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
        patch(f"{MODULE}.mq.publish", AsyncMock()) as mock_publish,
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
    assert mock_publish.await_args.args[1]["root_id"] == "om_from_row"


@pytest.mark.asyncio
async def test_submit_proactive_chat_ignores_target_from_other_chat():
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
        patch(f"{MODULE}.mq.publish", AsyncMock()) as mock_publish,
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
    assert mock_publish.await_args.args[1]["root_id"] == ""
