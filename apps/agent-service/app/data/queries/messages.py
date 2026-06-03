"""Chat message queries backed by common_* tables.

agent-service consumes ``common_message`` / ``common_conversation`` /
``common_agent_response`` only. The returned read model keeps the existing
agent-service payload names, where ``message_id`` is the common message id.
"""
from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import func, or_, update
from sqlalchemy.future import select

from app.data.message_record import CommonMessageRecord
from app.data.models import (
    CommonAgentResponse,
    CommonConversation,
    CommonMessage,
    CommonUser,
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
    "find_last_bot_reply_time",
    "find_context_messages_for_anchors",
    "find_gray_config",
    "find_user_messages_after",
    "find_proactive_messages_in_chat",
    "insert_proactive_message",
    "find_messages_with_user_chat_persona_by_root",
    "find_messages_with_user_chat_persona_in_chat",
    "update_messages_tos_files",
]


def _uuid(value: str | UUID | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _uuid_list(values: list[str] | set[str]) -> list[UUID]:
    out: list[UUID] = []
    for value in values:
        parsed = _uuid(value)
        if parsed is not None:
            out.append(parsed)
    return out


def _content_item_to_v2(item: dict) -> dict:
    if "type" in item:
        return item

    kind = item.get("kind")
    if kind == "text":
        return {"type": "text", "value": item.get("text", "")}
    if kind == "mention":
        out = {"type": "mention", "value": item.get("label") or item.get("id", "")}
        meta = dict(item.get("meta") or {})
        if item.get("id"):
            meta.setdefault("id", item["id"])
        if meta:
            out["meta"] = meta
        return out
    if kind in {"image", "audio", "file", "sticker"}:
        out = {"type": kind, "value": item.get("key", "")}
        if item.get("meta"):
            out["meta"] = item["meta"]
        return out
    if kind == "unsupported":
        return {
            "type": "unsupported",
            "value": item.get("text", ""),
            "meta": item.get("meta", {}),
        }
    return {"type": "unsupported", "value": str(item)}


def _content_text(content: list[dict], content_text: str | None) -> str:
    if content_text is not None:
        return content_text
    parts: list[str] = []
    for item in content:
        if item.get("kind") == "text":
            parts.append(str(item.get("text", "")))
        elif item.get("kind") == "mention":
            parts.append("@" + str(item.get("label") or item.get("id") or ""))
        elif item.get("type") == "text":
            parts.append(str(item.get("value", "")))
        elif item.get("type") == "mention":
            parts.append("@" + str(item.get("value", "")))
    return "".join(parts)


def _content_json(row: CommonMessage) -> str:
    content = row.content or []
    text = _content_text(content, row.content_text)
    return json.dumps(
        {
            "v": 2,
            "text": text,
            "items": [_content_item_to_v2(item) for item in content],
        },
        ensure_ascii=False,
    )


def _chat_type(scope: str) -> str:
    return "p2p" if scope == "direct" else scope


def _record(row: CommonMessage) -> CommonMessageRecord:
    return CommonMessageRecord(
        message_id=str(row.common_message_id),
        user_id=str(row.common_user_id) if row.common_user_id else None,
        username=row.sender_display_name,
        content=_content_json(row),
        role=row.role,
        root_message_id=str(row.common_root_message_id or row.common_message_id),
        reply_message_id=(
            str(row.common_reply_message_id) if row.common_reply_message_id else None
        ),
        chat_id=str(row.common_conversation_id),
        chat_type=_chat_type(row.scope),
        create_time=int(row.event_time),
        message_type=row.message_type,
        bot_name=row.bot_name,
        response_id=row.response_id,
    )


async def find_cross_chat_messages(
    user_id: str,
    bot_names: list[str],
    exclude_chat_id: str,
    since_ms: int,
    excluded_chat_ids: list[str] | None = None,
) -> list[CommonMessageRecord]:
    user_uuid = _uuid(user_id)
    exclude_chat_uuid = _uuid(exclude_chat_id)
    if user_uuid is None or exclude_chat_uuid is None:
        return []

    stmt = (
        select(CommonMessage)
        .where(CommonMessage.common_conversation_id != exclude_chat_uuid)
        .where(CommonMessage.event_time >= since_ms)
        .where(CommonMessage.bot_name.in_(bot_names))
        .where(
            or_(
                (CommonMessage.role == "user")
                & (CommonMessage.common_user_id == user_uuid),
                CommonMessage.role == "assistant",
            )
        )
        .order_by(CommonMessage.event_time.asc())
    )
    if excluded_chat_ids:
        excluded = _uuid_list(excluded_chat_ids)
        if excluded:
            stmt = stmt.where(~CommonMessage.common_conversation_id.in_(excluded))
    async with auto_tx():
        result = await current_session().execute(stmt)
        return [_record(row) for row in result.scalars().all()]


async def find_message_content(message_id: str) -> str | None:
    msg_uuid = _uuid(message_id)
    if msg_uuid is None:
        return None
    async with auto_tx():
        row = await current_session().scalar(
            select(CommonMessage).where(CommonMessage.common_message_id == msg_uuid)
        )
        return _content_json(row) if row else None


async def find_messages_in_range(
    chat_id: str,
    start_time: int,
    end_time: int,
    limit: int = 2000,
) -> list[CommonMessageRecord]:
    chat_uuid = _uuid(chat_id)
    if chat_uuid is None:
        return []
    async with auto_tx():
        result = await current_session().execute(
            select(CommonMessage)
            .where(CommonMessage.common_conversation_id == chat_uuid)
            .where(CommonMessage.event_time >= start_time)
            .where(CommonMessage.event_time < end_time)
            .order_by(CommonMessage.event_time.asc())
            .limit(limit)
        )
        return [_record(row) for row in result.scalars().all()]


async def find_username(user_id: str) -> str | None:
    user_uuid = _uuid(user_id)
    if user_uuid is None:
        return None
    async with auto_tx():
        result = await current_session().execute(
            select(CommonUser.display_name).where(CommonUser.common_user_id == user_uuid)
        )
        return result.scalar_one_or_none()


async def find_group_name(chat_id: str) -> str | None:
    chat_uuid = _uuid(chat_id)
    if chat_uuid is None:
        return None
    async with auto_tx():
        result = await current_session().execute(
            select(CommonConversation.display_name).where(
                CommonConversation.common_conversation_id == chat_uuid
            )
        )
        return result.scalar_one_or_none()


async def find_group_download_permission(chat_id: str) -> str | None:
    chat_uuid = _uuid(chat_id)
    if chat_uuid is None:
        return None
    async with auto_tx():
        result = await current_session().execute(
            select(CommonConversation.attachment_policy).where(
                CommonConversation.common_conversation_id == chat_uuid
            )
        )
        policy = result.scalar_one_or_none() or {}
        if policy.get("download_allowed") is True:
            return "all_messages"
        if policy.get("download_allowed") is False:
            return "not_allow"
        return None


async def find_message_by_id(message_id: str) -> CommonMessageRecord | None:
    msg_uuid = _uuid(message_id)
    if msg_uuid is None:
        return None
    async with auto_tx():
        result = await current_session().execute(
            select(CommonMessage).where(CommonMessage.common_message_id == msg_uuid)
        )
        row = result.scalar_one_or_none()
        return _record(row) if row else None


async def find_last_bot_reply_time(chat_id: str) -> int:
    chat_uuid = _uuid(chat_id)
    if chat_uuid is None:
        return 0
    async with auto_tx():
        result = await current_session().execute(
            select(func.max(CommonMessage.event_time)).where(
                CommonMessage.common_conversation_id == chat_uuid,
                CommonMessage.role == "assistant",
            )
        )
        return result.scalar_one_or_none() or 0


async def find_context_messages_for_anchors(
    chat_id: str,
    anchor_message_ids: list[str],
    anchor_timestamps: list[int],
    anchor_root_ids: set[str],
    context_window_ms: int = 300_000,
) -> list[tuple[CommonMessageRecord, str | None]]:
    chat_uuid = _uuid(chat_id)
    if chat_uuid is None:
        return []

    time_conditions = [
        CommonMessage.event_time.between(ts - context_window_ms, ts + context_window_ms)
        for ts in anchor_timestamps
        if ts
    ]
    message_ids = _uuid_list(anchor_message_ids)
    root_ids = _uuid_list(anchor_root_ids)
    or_conditions = [*time_conditions]
    if message_ids:
        or_conditions.append(CommonMessage.common_message_id.in_(message_ids))
    if root_ids:
        or_conditions.append(CommonMessage.common_root_message_id.in_(root_ids))
    if not or_conditions:
        return []

    stmt = (
        select(CommonMessage)
        .where(CommonMessage.common_conversation_id == chat_uuid, or_(*or_conditions))
        .order_by(CommonMessage.event_time.asc())
    )
    async with auto_tx():
        result = await current_session().execute(stmt)
        records = [_record(row) for row in result.scalars().all()]
        return [(record, record.username) for record in records]


async def find_gray_config(message_id: str) -> dict | None:
    msg_uuid = _uuid(message_id)
    if msg_uuid is None:
        return None
    async with auto_tx():
        row = await current_session().scalar(
            select(CommonMessage).where(CommonMessage.common_message_id == msg_uuid)
        )
        if not row:
            return None
        conversation = await current_session().scalar(
            select(CommonConversation).where(
                CommonConversation.common_conversation_id
                == row.common_conversation_id
            )
        )
        policy = conversation.attachment_policy if conversation else None
        gray = (policy or {}).get("gray_config")
        return gray if isinstance(gray, dict) else None


async def find_user_messages_after(
    chat_id: str,
    *,
    after: int,
    limit: int,
    exclude_user_id: str,
) -> list[CommonMessageRecord]:
    chat_uuid = _uuid(chat_id)
    exclude_user_uuid = _uuid(exclude_user_id)
    if chat_uuid is None:
        return []

    stmt = (
        select(CommonMessage)
        .where(
            CommonMessage.common_conversation_id == chat_uuid,
            CommonMessage.role == "user",
            CommonMessage.message_type != "proactive_trigger",
            CommonMessage.event_time > after,
        )
        .order_by(CommonMessage.event_time.desc())
        .limit(limit)
    )
    if exclude_user_uuid is not None:
        stmt = stmt.where(CommonMessage.common_user_id != exclude_user_uuid)

    async with auto_tx():
        result = await current_session().execute(stmt)
        return [_record(row) for row in result.scalars().all()]


async def find_proactive_messages_in_chat(
    chat_id: str,
    *,
    bot_name: str,
    proactive_user_id: str,
    since_ms: int,
) -> list[CommonMessageRecord]:
    chat_uuid = _uuid(chat_id)
    if chat_uuid is None:
        return []

    stmt = (
        select(CommonMessage)
        .where(
            CommonMessage.common_conversation_id == chat_uuid,
            CommonMessage.message_type == "proactive_trigger",
            CommonMessage.bot_name == bot_name,
            CommonMessage.event_time >= since_ms,
        )
        .order_by(CommonMessage.event_time.desc())
    )
    async with auto_tx():
        result = await current_session().execute(stmt)
        return [_record(row) for row in result.scalars().all()]


async def insert_proactive_message(message: CommonMessage) -> None:
    async with auto_tx():
        current_session().add(message)


async def find_messages_with_user_chat_persona_by_root(
    *,
    root_message_id: str,
    until_create_time: int,
) -> list[tuple[CommonMessageRecord, str | None, str | None, str | None]]:
    root_uuid = _uuid(root_message_id)
    if root_uuid is None:
        return []

    stmt = (
        select(
            CommonMessage,
            CommonConversation.display_name.label("chat_name"),
            CommonAgentResponse.persona_id.label("persona_id"),
        )
        .outerjoin(
            CommonConversation,
            CommonMessage.common_conversation_id
            == CommonConversation.common_conversation_id,
        )
        .outerjoin(
            CommonAgentResponse,
            CommonMessage.response_id == CommonAgentResponse.session_id,
        )
        .where(CommonMessage.common_root_message_id == root_uuid)
        .where(CommonMessage.event_time <= until_create_time)
        .order_by(CommonMessage.event_time.asc())
    )
    async with auto_tx():
        result = await current_session().execute(stmt)
        rows = []
        for msg, chat_name, persona_id in result.all():
            record = _record(msg)
            rows.append((record, record.username, chat_name, persona_id))
        return rows


async def find_messages_with_user_chat_persona_in_chat(
    *,
    chat_id: str,
    exclude_root_message_id: str,
    after_create_time: int,
    before_create_time: int,
    exclude_user_id: str,
    limit: int,
) -> list[tuple[CommonMessageRecord, str | None, str | None, str | None]]:
    chat_uuid = _uuid(chat_id)
    root_uuid = _uuid(exclude_root_message_id)
    exclude_user_uuid = _uuid(exclude_user_id)
    if chat_uuid is None or root_uuid is None:
        return []

    stmt = (
        select(
            CommonMessage,
            CommonConversation.display_name.label("chat_name"),
            CommonAgentResponse.persona_id.label("persona_id"),
        )
        .outerjoin(
            CommonConversation,
            CommonMessage.common_conversation_id
            == CommonConversation.common_conversation_id,
        )
        .outerjoin(
            CommonAgentResponse,
            CommonMessage.response_id == CommonAgentResponse.session_id,
        )
        .where(
            CommonMessage.common_conversation_id == chat_uuid,
            CommonMessage.common_root_message_id != root_uuid,
            CommonMessage.event_time >= after_create_time,
            CommonMessage.event_time < before_create_time,
        )
        .order_by(CommonMessage.event_time.desc())
        .limit(limit)
    )
    if exclude_user_uuid is not None:
        stmt = stmt.where(CommonMessage.common_user_id != exclude_user_uuid)

    async with auto_tx():
        result = await current_session().execute(stmt)
        rows = []
        for msg, chat_name, persona_id in result.all():
            record = _record(msg)
            rows.append((record, record.username, chat_name, persona_id))
        return rows


async def update_messages_tos_files(
    updates: dict[str, dict[str, str]],
) -> int:
    if not updates:
        return 0

    from app.chat.content_parser import update_tos_files

    updated_count = 0
    async with auto_tx():
        s = current_session()
        for mid, mapping in updates.items():
            msg_uuid = _uuid(mid)
            if msg_uuid is None:
                continue
            row = await s.scalar(
                select(CommonMessage).where(CommonMessage.common_message_id == msg_uuid)
            )
            if row is None:
                continue
            new_content = update_tos_files(_content_json(row), mapping)
            if not new_content:
                continue
            data = json.loads(new_content)
            row.content = data.get("items", [])
            row.content_text = data.get("text")
            await s.execute(
                update(CommonMessage)
                .where(CommonMessage.common_message_id == msg_uuid)
                .values(content=row.content, content_text=row.content_text)
            )
            updated_count += 1
    return updated_count
