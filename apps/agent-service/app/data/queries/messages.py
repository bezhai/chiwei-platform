"""Chat message queries — conversation_messages + lark_user / lark_group_*.

Operates on tables: ``ConversationMessage``, ``LarkUser``,
``LarkGroupChatInfo``, ``LarkGroupMember``, ``LarkBaseChatInfo``.
"""
from __future__ import annotations

from sqlalchemy import func, or_
from sqlalchemy.future import select

from app.data.models import (
    ConversationMessage,
    LarkBaseChatInfo,
    LarkGroupChatInfo,
    LarkGroupMember,
    LarkUser,
)
from app.runtime.db import auto_tx, current_session

__all__ = [
    "find_cross_chat_messages",
    "find_message_content",
    "find_messages_in_range",
    "find_username",
    "find_group_name",
    "find_group_download_permission",
    "find_message_by_id",
    "resolve_message_id_by_row_id",
    "find_last_bot_reply_time",
    "find_context_messages_for_anchors",
    "find_group_members",
    "find_gray_config",
]


async def find_cross_chat_messages(
    user_id: str,
    bot_names: list[str],
    exclude_chat_id: str,
    since_ms: int,
    excluded_chat_ids: list[str] | None = None,
) -> list[ConversationMessage]:
    """Fetch recent cross-chat interactions between a user and a persona.

    Returns user messages + bot replies across all chat types (group + p2p).
    Excludes the current chat and any blacklisted chat IDs.
    """
    stmt = (
        select(ConversationMessage)
        .where(ConversationMessage.chat_id != exclude_chat_id)
        .where(ConversationMessage.create_time >= since_ms)
        .where(ConversationMessage.bot_name.in_(bot_names))
        .where(
            or_(
                # User's messages
                (ConversationMessage.role == "user")
                & (ConversationMessage.user_id == user_id),
                # Bot's assistant replies
                ConversationMessage.role == "assistant",
            )
        )
        .order_by(ConversationMessage.create_time.asc())
    )
    if excluded_chat_ids:
        stmt = stmt.where(~ConversationMessage.chat_id.in_(excluded_chat_ids))
    async with auto_tx():
        result = await current_session().execute(stmt)
        return list(result.scalars().all())


async def find_message_content(message_id: str) -> str | None:
    """Fetch message content by message_id."""
    stmt = select(ConversationMessage.content).where(
        ConversationMessage.message_id == message_id
    )
    async with auto_tx():
        return await current_session().scalar(stmt)


async def find_messages_in_range(
    chat_id: str,
    start_time: int,
    end_time: int,
    limit: int = 2000,
) -> list[ConversationMessage]:
    """Fetch messages in a chat within a time range (ascending)."""
    async with auto_tx():
        result = await current_session().execute(
            select(ConversationMessage)
            .where(ConversationMessage.chat_id == chat_id)
            .where(ConversationMessage.create_time >= start_time)
            .where(ConversationMessage.create_time < end_time)
            .order_by(ConversationMessage.create_time.asc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def find_username(user_id: str) -> str | None:
    """Look up display name from lark_user by union_id."""
    async with auto_tx():
        result = await current_session().execute(
            select(LarkUser.name).where(LarkUser.union_id == user_id)
        )
        return result.scalar_one_or_none()


async def find_group_name(chat_id: str) -> str | None:
    """Look up group name from lark_group_chat_info."""
    async with auto_tx():
        result = await current_session().execute(
            select(LarkGroupChatInfo.name).where(LarkGroupChatInfo.chat_id == chat_id)
        )
        return result.scalar_one_or_none()


async def find_group_download_permission(chat_id: str) -> str | None:
    """Fetch download_has_permission_setting for a group chat, or None."""
    async with auto_tx():
        result = await current_session().execute(
            select(LarkGroupChatInfo.download_has_permission_setting).where(
                LarkGroupChatInfo.chat_id == chat_id
            )
        )
        return result.scalar_one_or_none()


async def find_message_by_id(message_id: str) -> ConversationMessage | None:
    """Fetch full message object by message_id."""
    async with auto_tx():
        result = await current_session().execute(
            select(ConversationMessage).where(ConversationMessage.message_id == message_id)
        )
        return result.scalar_one_or_none()


async def resolve_message_id_by_row_id(row_id: str | int) -> str | None:
    """Resolve int row id to lark message_id (om_xxx). Returns None if not found."""
    try:
        rid = int(row_id)
    except (ValueError, TypeError):
        return None
    async with auto_tx():
        result = await current_session().execute(
            select(ConversationMessage.message_id).where(ConversationMessage.id == rid)
        )
        return result.scalar_one_or_none()


async def find_last_bot_reply_time(chat_id: str) -> int:
    """Return the latest assistant reply create_time (ms) in a chat, or 0."""
    async with auto_tx():
        result = await current_session().execute(
            select(func.max(ConversationMessage.create_time)).where(
                ConversationMessage.chat_id == chat_id,
                ConversationMessage.role == "assistant",
            )
        )
        return result.scalar_one_or_none() or 0


async def find_context_messages_for_anchors(
    chat_id: str,
    anchor_message_ids: list[str],
    anchor_timestamps: list[int],
    anchor_root_ids: set[str],
    context_window_ms: int = 300_000,
) -> list[tuple[ConversationMessage, LarkUser]]:
    """Find messages surrounding anchor points (for search_group_history).

    Returns list of (ConversationMessage, LarkUser) tuples.
    """
    time_conditions = [
        ConversationMessage.create_time.between(
            ts - context_window_ms, ts + context_window_ms
        )
        for ts in anchor_timestamps
        if ts
    ]
    or_conditions = [
        *time_conditions,
        ConversationMessage.message_id.in_(anchor_message_ids),
    ]
    if anchor_root_ids:
        or_conditions.append(
            ConversationMessage.root_message_id.in_(anchor_root_ids)
        )

    stmt = (
        select(ConversationMessage, LarkUser)
        .join(LarkUser, ConversationMessage.user_id == LarkUser.union_id)
        .where(
            ConversationMessage.chat_id == chat_id,
            or_(*or_conditions),
        )
        .order_by(ConversationMessage.create_time.asc())
    )
    async with auto_tx():
        result = await current_session().execute(stmt)
        return list(result.all())


async def find_group_members(
    chat_id: str,
    role: str | None = None,
) -> list[tuple[LarkGroupMember, LarkUser]]:
    """Find group members with user info.

    Returns list of (LarkGroupMember, LarkUser) tuples.
    """
    stmt = (
        select(LarkGroupMember, LarkUser)
        .join(LarkUser, LarkGroupMember.union_id == LarkUser.union_id)
        .where(
            LarkGroupMember.chat_id == chat_id,
            ~LarkGroupMember.is_leave,
        )
    )
    if role == "owner":
        stmt = stmt.where(LarkGroupMember.is_owner)
    elif role == "manager":
        stmt = stmt.where(LarkGroupMember.is_manager)

    async with auto_tx():
        result = await current_session().execute(stmt)
        return list(result.all())


async def find_gray_config(message_id: str) -> dict | None:
    """Look up gray_config for the chat that a message belongs to."""
    stmt = (
        select(LarkBaseChatInfo.gray_config)
        .join(
            ConversationMessage,
            ConversationMessage.chat_id == LarkBaseChatInfo.chat_id,
        )
        .where(ConversationMessage.message_id == message_id)
    )
    async with auto_tx():
        return await current_session().scalar(stmt)
