from unittest.mock import AsyncMock, patch

import pytest

from app.data.models import ConversationMessage


@pytest.mark.asyncio
async def test_emit_legacy_message_lifts_every_field_and_emits():
    from app.bridges.message_bridge import emit_legacy_message

    cm = ConversationMessage(
        message_id="m1",
        user_id="u1",
        content="hello",
        role="user",
        root_message_id="r1",
        reply_message_id=None,
        chat_id="c1",
        chat_type="p2p",
        create_time=1234567890,
        message_type="text",
        bot_name=None,
        response_id=None,
    )

    with patch("app.bridges.message_bridge.emit", new_callable=AsyncMock) as m:
        await emit_legacy_message(cm)

    m.assert_awaited_once()
    msg = m.call_args.args[0]
    from app.domain.message import Message
    assert isinstance(msg, Message)
    # Spot-check every field is carried through (not defaulted)
    assert msg.message_id == "m1"
    assert msg.user_id == "u1"
    assert msg.content == "hello"
    assert msg.role == "user"
    assert msg.root_message_id == "r1"
    assert msg.reply_message_id is None
    assert msg.chat_id == "c1"
    assert msg.chat_type == "p2p"
    assert msg.create_time == 1234567890
    assert msg.message_type == "text"
    assert msg.bot_name is None
    assert msg.response_id is None
