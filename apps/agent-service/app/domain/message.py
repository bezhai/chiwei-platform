"""Message Data — takes over the legacy conversation_messages table.

Fields mirror ``app.data.models.ConversationMessage`` 1:1 (minus the auto-
increment ``id`` column, which the migrator's adoption-mode ignores). The
table is owned by pre-existing migrations; this Data class is the new typed
interface for reading/writing through runtime.persist / runtime.query.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from app.runtime.data import Data, Key

if TYPE_CHECKING:
    from app.data.models import ConversationMessage


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
    bot_name: str | None = None
    response_id: str | None = None

    class Meta:
        existing_table = "conversation_messages"
        # Real PK is ``message_id``; there is no ``dedup_hash`` column,
        # so the persist layer must ON CONFLICT on message_id instead.
        dedup_column = "message_id"

    @classmethod
    def from_cm(cls, cm: "ConversationMessage") -> "Message":
        """Lift a legacy ``ConversationMessage`` ORM row into a ``Message`` Data."""
        return cls(
            message_id=cm.message_id,
            user_id=cm.user_id,
            content=cm.content,
            role=cm.role,
            root_message_id=cm.root_message_id,
            reply_message_id=cm.reply_message_id,
            chat_id=cm.chat_id,
            chat_type=cm.chat_type,
            create_time=cm.create_time,
            message_type=cm.message_type,
            bot_name=cm.bot_name,
            response_id=cm.response_id,
        )
