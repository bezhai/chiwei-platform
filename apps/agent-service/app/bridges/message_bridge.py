"""Message Bridge: lift legacy ConversationMessage rows into new Message Data.

Exists during Phases 1-4. After Phase 5 the call sites are deleted along with
this file. Field mapping is straight pass-through — Message was defined to
mirror ConversationMessage 1:1, so there is no rename or coercion here.
"""
from __future__ import annotations

from app.data.models import ConversationMessage
from app.domain.message import Message
from app.runtime.emit import emit


async def emit_legacy_message(cm: ConversationMessage) -> None:
    await emit(
        Message(
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
            vector_status=cm.vector_status,
            bot_name=cm.bot_name,
            response_id=cm.response_id,
        )
    )
