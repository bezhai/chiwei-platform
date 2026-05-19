"""Chat message queries — conversation_messages + lark_user / lark_group_*.

Operates on tables: ``ConversationMessage``, ``LarkUser``,
``LarkGroupChatInfo``, ``LarkGroupMember``, ``LarkBaseChatInfo``,
``agent_responses`` (raw subquery — no ORM model).
"""
from __future__ import annotations

from sqlalchemy import func, literal_column, or_, text
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
    "find_user_messages_after",
    "find_proactive_messages_in_chat",
    "insert_proactive_message",
    "find_messages_with_user_chat_persona_by_root",
    "find_messages_with_user_chat_persona_in_chat",
    "update_messages_tos_files",
]


def _agent_responses_subquery():
    """Build subquery exposing ``agent_responses.session_id`` + ``persona_id``.

    ``agent_responses`` has no ORM model, so we hand-roll a subquery via
    ``literal_column`` and ``text``. Centralized here to keep the hack
    out of the business layer.
    """
    return (
        select(
            literal_column("agent_responses.session_id").label("ar_session_id"),
            literal_column("agent_responses.persona_id").label("ar_persona_id"),
        )
        .select_from(text("agent_responses"))
        .subquery("ar")
    )


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
    """Look up sender display name for a (global) user_id.

    身份全局化后 ``user_id`` 是全局 internal_user_id，不再能 JOIN
    ``lark_user.union_id``。显示名作为冗余列随消息写入
    ``conversation_messages.username``，这里取该 user 最近一条消息上
    的 username（无 lark_user JOIN、无 COALESCE fallback）。
    """
    async with auto_tx():
        result = await current_session().execute(
            select(ConversationMessage.username)
            .where(ConversationMessage.user_id == user_id)
            .where(ConversationMessage.username.is_not(None))
            .order_by(ConversationMessage.create_time.desc())
            .limit(1)
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
) -> list[tuple[ConversationMessage, str | None]]:
    """Find messages surrounding anchor points (for search_group_history).

    身份全局化后不再 JOIN lark_user 取名，发送者显示名直接读
    ``conversation_messages.username`` 冗余列。

    Returns list of ``(ConversationMessage, username | None)`` tuples.
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
        select(ConversationMessage, ConversationMessage.username)
        .where(
            ConversationMessage.chat_id == chat_id,
            or_(*or_conditions),
        )
        .order_by(ConversationMessage.create_time.asc())
    )
    async with auto_tx():
        result = await current_session().execute(stmt)
        return [(row[0], row[1]) for row in result.all()]


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


async def find_user_messages_after(
    chat_id: str,
    *,
    after: int,
    limit: int,
    exclude_user_id: str,
) -> list[ConversationMessage]:
    """Fetch user messages in a chat newer than *after* (ms), descending.

    Used by Glimpse/proactive to get the most recent unseen user
    messages. Caller is expected to ``reverse()`` for chronological
    order if needed.
    """
    stmt = (
        select(ConversationMessage)
        .where(
            ConversationMessage.chat_id == chat_id,
            ConversationMessage.role == "user",
            ConversationMessage.user_id != exclude_user_id,
            ConversationMessage.create_time > after,
        )
        .order_by(ConversationMessage.create_time.desc())
        .limit(limit)
    )
    async with auto_tx():
        result = await current_session().execute(stmt)
        return list(result.scalars().all())


async def find_proactive_messages_in_chat(
    chat_id: str,
    *,
    bot_name: str,
    proactive_user_id: str,
    since_ms: int,
) -> list[ConversationMessage]:
    """Fetch proactive trigger messages for a persona in a chat since *since_ms*.

    Returns rows in descending create_time order. Business layer is
    responsible for projection (parse_content / time formatting).
    """
    stmt = (
        select(ConversationMessage)
        .where(
            ConversationMessage.chat_id == chat_id,
            ConversationMessage.user_id == proactive_user_id,
            ConversationMessage.bot_name == bot_name,
            ConversationMessage.create_time >= since_ms,
        )
        .order_by(ConversationMessage.create_time.desc())
    )
    async with auto_tx():
        result = await current_session().execute(stmt)
        return list(result.scalars().all())


async def insert_proactive_message(message: ConversationMessage) -> None:
    """Persist a proactive trigger ``ConversationMessage`` entity.

    Caller constructs the entity (proactive submit needs the full row to
    pass into ``Message.from_cm`` for the outbox emit). This query just
    owns the ``session.add`` so business code never touches the session.
    """
    async with auto_tx():
        current_session().add(message)


async def find_messages_with_user_chat_persona_by_root(
    *,
    root_message_id: str,
    until_create_time: int,
) -> list[tuple[ConversationMessage, str | None, str | None, str | None]]:
    """Quick-search root chain query.

    Fetch all messages sharing ``root_message_id`` with create_time
    <= ``until_create_time``, with sender display name read from the
    ``conversation_messages.username`` redundant column (身份全局化后
    ``user_id`` 是全局 internal_user_id，不再 JOIN lark_user.union_id),
    plus ``LarkGroupChatInfo.name`` and the ``agent_responses.persona_id``
    via response_id. Ordered by create_time ascending.

    Returns ``(message, username, chat_name, persona_id)`` tuples.
    """
    ar = _agent_responses_subquery()
    stmt = (
        select(
            ConversationMessage,
            ConversationMessage.username.label("username"),
            LarkGroupChatInfo.name.label("chat_name"),
            ar.c.ar_persona_id.label("persona_id"),
        )
        .outerjoin(
            LarkGroupChatInfo,
            ConversationMessage.chat_id == LarkGroupChatInfo.chat_id,
        )
        .outerjoin(ar, ConversationMessage.response_id == ar.c.ar_session_id)
        .where(ConversationMessage.root_message_id == root_message_id)
        .where(ConversationMessage.create_time <= until_create_time)
        .order_by(ConversationMessage.create_time.asc())
    )
    async with auto_tx():
        result = await current_session().execute(stmt)
        return [(row[0], row[1], row[2], row[3]) for row in result.all()]


async def find_messages_with_user_chat_persona_in_chat(
    *,
    chat_id: str,
    exclude_root_message_id: str,
    after_create_time: int,
    before_create_time: int,
    exclude_user_id: str,
    limit: int,
) -> list[tuple[ConversationMessage, str | None, str | None, str | None]]:
    """Quick-search supplemental window query.

    Fetch messages in *chat_id* outside of *exclude_root_message_id*'s
    chain, within ``[after_create_time, before_create_time)``, excluding
    *exclude_user_id*. Sender display name read from the
    ``conversation_messages.username`` redundant column (身份全局化后
    不再 JOIN lark_user.union_id), joined with chat/agent_responses like
    the root query. Ordered by create_time descending, capped at *limit*.

    Returns ``(message, username, chat_name, persona_id)`` tuples in
    the same shape as ``find_messages_with_user_chat_persona_by_root``.
    """
    ar = _agent_responses_subquery()
    stmt = (
        select(
            ConversationMessage,
            ConversationMessage.username.label("username"),
            LarkGroupChatInfo.name.label("chat_name"),
            ar.c.ar_persona_id.label("persona_id"),
        )
        .outerjoin(
            LarkGroupChatInfo,
            ConversationMessage.chat_id == LarkGroupChatInfo.chat_id,
        )
        .outerjoin(ar, ConversationMessage.response_id == ar.c.ar_session_id)
        .where(
            ConversationMessage.chat_id == chat_id,
            ConversationMessage.root_message_id != exclude_root_message_id,
            ConversationMessage.create_time >= after_create_time,
            ConversationMessage.create_time < before_create_time,
            ConversationMessage.user_id != exclude_user_id,
        )
        .order_by(ConversationMessage.create_time.desc())
        .limit(limit)
    )
    async with auto_tx():
        result = await current_session().execute(stmt)
        return [(row[0], row[1], row[2], row[3]) for row in result.all()]


async def update_messages_tos_files(
    updates: dict[str, dict[str, str]],
) -> int:
    """Apply tos_file mappings into ``ConversationMessage.content`` rows.

    *updates* maps ``message_id -> {image_key: tos_file_id}``. For each
    message we read the row, apply ``update_tos_files`` to merge the
    mapping into the v2 content JSON, and write it back. Single tx so
    the whole batch commits atomically.

    Returns the count of messages actually updated (i.e. content
    changed). Missing rows or no-op merges silently skip.
    """
    if not updates:
        return 0

    # Local import: ``app.chat`` package init transitively imports
    # ``app.data.queries``, so a top-level import here would cycle.
    from app.chat.content_parser import update_tos_files

    updated_count = 0
    async with auto_tx():
        s = current_session()
        for mid, mapping in updates.items():
            row = await s.scalar(
                select(ConversationMessage).where(
                    ConversationMessage.message_id == mid
                )
            )
            if row is None:
                continue
            new_content = update_tos_files(row.content, mapping)
            if new_content:
                row.content = new_content
                updated_count += 1
    return updated_count
