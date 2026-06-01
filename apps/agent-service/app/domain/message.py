"""Message Data used inside agent-service.

``message_id`` is the common message id. This Data object is transient; DB
reads are owned by ``app.data.queries.messages`` against ``common_message``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from app.runtime import Data, Key

if TYPE_CHECKING:
    from app.data.message_record import CommonMessageRecord


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
        transient = True

    @classmethod
    def from_record(cls, cm: CommonMessageRecord) -> Message:
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
