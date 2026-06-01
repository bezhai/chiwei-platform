"""Common message read model for agent-service.

The database source is ``common_message``. Field names here match the
agent-service domain payloads where ``message_id`` means ``common_message_id``
and ``chat_id`` means ``common_conversation_id``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CommonMessageRecord:
    message_id: str
    user_id: str | None
    username: str | None
    content: str
    role: str
    root_message_id: str
    reply_message_id: str | None
    chat_id: str
    chat_type: str
    create_time: int
    message_type: str | None = "text"
    bot_name: str | None = None
    response_id: str | None = None
