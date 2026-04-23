"""Message Data — takes over the legacy conversation_messages table.

Fields mirror ``app.data.models.ConversationMessage`` 1:1 (minus the auto-
increment ``id`` column, which the migrator's adoption-mode ignores). The
table is owned by pre-existing migrations; this Data class is the new typed
interface for reading/writing through runtime.persist / runtime.query.
"""
from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key


class Message(Data):
    message_id: Annotated[str, Key]
    user_id: str
    content: str
    role: str
    root_message_id: str
    reply_message_id: str | None = None
    chat_id: str
    chat_type: str
    create_time: int
    message_type: str | None = "text"
    vector_status: str = "pending"
    bot_name: str | None = None
    response_id: str | None = None

    class Meta:
        existing_table = "conversation_messages"
        # Real PK is ``message_id``; there is no ``dedup_hash`` column,
        # so the persist layer must ON CONFLICT on message_id instead.
        dedup_column = "message_id"
