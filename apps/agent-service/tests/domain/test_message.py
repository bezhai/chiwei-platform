"""Message Data class — adopts the pre-existing conversation_messages table.

The table is owned by legacy migrations; Message is declared in adoption
mode (``Meta.existing_table``) so the migrator emits no DDL for it.
Dedup must use the real PK column (``message_id``) rather than the
runtime-managed ``dedup_hash`` (which the legacy table does not have).
"""
from __future__ import annotations

from app.domain.message import Message
from app.runtime.data import key_fields


def test_message_key_is_message_id():
    assert key_fields(Message) == ("message_id",)


def test_message_dedup_column_is_message_id():
    assert Message.Meta.dedup_column == "message_id"


def test_message_existing_table():
    assert Message.Meta.existing_table == "conversation_messages"


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
