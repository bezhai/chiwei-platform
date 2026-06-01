"""Message Data class.

Message is a transient in-process view hydrated from ``common_message`` by the
data query layer. The runtime must not create or adopt a DB table for it.
"""
from __future__ import annotations

from app.domain.message import Message
from app.runtime.data import key_fields


def test_message_key_is_message_id():
    assert key_fields(Message) == ("message_id",)


def test_message_is_transient():
    assert Message.Meta.transient is True


def test_message_instance_matches_real_schema():
    m = Message(
        message_id="m1",
        user_id="u1",
        content="hi",
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
    assert m.message_id == "m1"
    assert m.content == "hi"
