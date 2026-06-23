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


@dataclass(slots=True)
class LifeChatMessage:
    """life 醒来读对话时的一条消息 —— 已经判明「谁说的」的可读形态。

    ``is_self`` = 这条是不是赤尾自己说的（role=assistant 且发言 persona == 当前
    persona）；``speaker_display_name`` 是发言者展示名（真人私聊用昵称兜底、不暴露
    raw user_id）；``cst_time`` 是 CST 显示串（``HH:MM CST``），不是裸毫秒。
    """

    speaker_display_name: str
    is_self: bool
    text: str
    cst_time: str


@dataclass(slots=True)
class LifeChatConversation:
    """life 醒来读对话时的一个会话分组 —— 一个会话 + 它最近一段消息。

    ``scope`` = ``"direct"``（私聊）/ ``"group"``（群）；``display_name`` 是群名，
    私聊为 ``None``；``messages`` 按发生先后升序。
    """

    chat_id: str
    scope: str
    display_name: str | None
    messages: list[LifeChatMessage]
