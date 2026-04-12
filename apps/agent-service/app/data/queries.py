"""Domain queries — pure data access, no business logic.

All functions accept ``session: AsyncSession`` as the first parameter.
Callers manage transactions via ``get_session()`` context manager.

Sections:
  - Model Provider
  - Persona / Bot config
  - Chat messages
  - Schedule
  - Life Engine
  - Memory — fragments, glimpse, reply style, relationship
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, or_, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.data.models import (
    AkaoSchedule,
    BotPersona,
    ConversationMessage,
    ExperienceFragment,
    GlimpseState,
    LarkBaseChatInfo,
    LarkGroupChatInfo,
    LarkGroupMember,
    LarkUser,
    LifeEngineState,
    ModelMapping,
    ModelProvider,
    RelationshipMemoryV2,
    ReplyStyleLog,
)

# CST timezone for date boundary calculations
_CST = timezone(timedelta(hours=8))


# --- Model Provider ---


def parse_model_id(model_id: str) -> tuple[str, str]:
    """Parse ``"provider:model"`` into ``(provider_name, model_name)``.

    Falls back to ``"302.ai"`` as default provider when no colon present.
    """
    if ":" in model_id:
        provider_name, model_name = model_id.split(":", 1)
        return provider_name.strip(), model_name.strip()
    return "302.ai", model_id.strip()


async def find_model_mapping(session: AsyncSession, alias: str) -> ModelMapping | None:
    """Look up a model mapping by alias."""
    result = await session.execute(
        select(ModelMapping).where(ModelMapping.alias == alias)
    )
    return result.scalar_one_or_none()


async def find_provider_by_name(
    session: AsyncSession, name: str
) -> ModelProvider | None:
    """Look up a model provider by name."""
    result = await session.execute(
        select(ModelProvider).where(ModelProvider.name == name)
    )
    return result.scalar_one_or_none()


# --- Persona / Bot config ---


async def find_persona(session: AsyncSession, persona_id: str) -> BotPersona | None:
    """Fetch a bot persona by primary key."""
    return await session.get(BotPersona, persona_id)


async def list_all_persona_ids(session: AsyncSession) -> list[str]:
    """Return all persona_id values from bot_persona table."""
    result = await session.execute(select(BotPersona.persona_id))
    return [row[0] for row in result.all()]


async def find_gray_config(session: AsyncSession, message_id: str) -> dict | None:
    """Look up gray_config for the chat that a message belongs to."""
    stmt = (
        select(LarkBaseChatInfo.gray_config)
        .join(
            ConversationMessage,
            ConversationMessage.chat_id == LarkBaseChatInfo.chat_id,
        )
        .where(ConversationMessage.message_id == message_id)
    )
    return await session.scalar(stmt)


async def resolve_persona_id(session: AsyncSession, bot_name: str) -> str:
    """Map bot_name -> persona_id via bot_config table. Falls back to bot_name itself."""
    result = await session.execute(
        text("SELECT persona_id FROM bot_config WHERE bot_name = :bn"),
        {"bn": bot_name},
    )
    row = result.scalar_one_or_none()
    return row if row else bot_name


async def resolve_bot_name_for_persona(
    session: AsyncSession, persona_id: str, chat_id: str
) -> str | None:
    """Find the bot_name that should send messages for a persona in a specific chat."""
    result = await session.execute(
        text(
            "SELECT bc.bot_name FROM bot_config bc "
            "JOIN bot_chat_presence bp ON bc.bot_name = bp.bot_name "
            "WHERE bp.chat_id = :cid AND bp.is_active = true "
            "AND bc.persona_id = :pid AND bc.is_active = true "
            "LIMIT 1"
        ),
        {"cid": chat_id, "pid": persona_id},
    )
    return result.scalar_one_or_none()


async def resolve_mentioned_personas(
    session: AsyncSession, mentions: list[str]
) -> list[str]:
    """Map mention app_id list to persona_id list via bot_config table."""
    result = await session.execute(
        text(
            "SELECT DISTINCT persona_id FROM bot_config "
            "WHERE app_id = ANY(:mentions) "
            "AND is_active = true "
            "AND persona_id IS NOT NULL"
        ),
        {"mentions": mentions},
    )
    return [row[0] for row in result.fetchall()]


# --- Chat messages ---


async def find_message_content(session: AsyncSession, message_id: str) -> str | None:
    """Fetch message content by message_id."""
    stmt = select(ConversationMessage.content).where(
        ConversationMessage.message_id == message_id
    )
    return await session.scalar(stmt)


async def find_messages_in_range(
    session: AsyncSession,
    chat_id: str,
    start_time: int,
    end_time: int,
    limit: int = 2000,
) -> list[ConversationMessage]:
    """Fetch messages in a chat within a time range (ascending)."""
    result = await session.execute(
        select(ConversationMessage)
        .where(ConversationMessage.chat_id == chat_id)
        .where(ConversationMessage.create_time >= start_time)
        .where(ConversationMessage.create_time < end_time)
        .order_by(ConversationMessage.create_time.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def find_username(session: AsyncSession, user_id: str) -> str | None:
    """Look up display name from lark_user by union_id."""
    result = await session.execute(
        select(LarkUser.name).where(LarkUser.union_id == user_id)
    )
    return result.scalar_one_or_none()


async def find_group_name(session: AsyncSession, chat_id: str) -> str | None:
    """Look up group name from lark_group_chat_info."""
    result = await session.execute(
        select(LarkGroupChatInfo.name).where(LarkGroupChatInfo.chat_id == chat_id)
    )
    return result.scalar_one_or_none()


async def find_group_download_permission(
    session: AsyncSession, chat_id: str
) -> str | None:
    """Fetch download_has_permission_setting for a group chat, or None."""
    result = await session.execute(
        select(LarkGroupChatInfo.download_has_permission_setting).where(
            LarkGroupChatInfo.chat_id == chat_id
        )
    )
    return result.scalar_one_or_none()


async def find_message_by_id(
    session: AsyncSession, message_id: str
) -> ConversationMessage | None:
    """Fetch full message object by message_id."""
    result = await session.execute(
        select(ConversationMessage).where(ConversationMessage.message_id == message_id)
    )
    return result.scalar_one_or_none()


async def set_vector_status(
    session: AsyncSession, message_id: str, status: str
) -> None:
    """Update vectorization status for a message."""
    await session.execute(
        update(ConversationMessage)
        .where(ConversationMessage.message_id == message_id)
        .values(vector_status=status)
    )


async def scan_pending_messages(
    session: AsyncSession,
    cutoff_ts: int,
    offset: int,
    limit: int,
) -> list[str]:
    """Scan message IDs with pending vector status since cutoff timestamp."""
    result = await session.execute(
        select(ConversationMessage.message_id)
        .where(ConversationMessage.vector_status == "pending")
        .where(ConversationMessage.create_time >= cutoff_ts)
        .order_by(ConversationMessage.create_time.desc())
        .offset(offset)
        .limit(limit)
    )
    return [row[0] for row in result.fetchall()]


async def set_agent_response_bot(
    session: AsyncSession,
    session_id: str,
    bot_name: str,
    persona_id: str,
) -> None:
    """Update bot_name and persona_id on agent_responses row."""
    await session.execute(
        text(
            "UPDATE agent_responses SET bot_name = :bn, persona_id = :pid "
            "WHERE session_id = :sid"
        ),
        {"bn": bot_name, "pid": persona_id, "sid": session_id},
    )


async def set_safety_status(
    session: AsyncSession,
    session_id: str,
    status: str,
    result_json: dict | None = None,
) -> None:
    """Update safety_status (and optional result) on agent_responses row."""
    await session.execute(
        text(
            "UPDATE agent_responses "
            "SET safety_status = :status, "
            "    safety_result = CAST(:result AS jsonb), "
            "    updated_at = NOW() "
            "WHERE session_id = :session_id"
        ),
        {
            "status": status,
            "result": (json.dumps(result_json) if result_json else None),
            "session_id": session_id,
        },
    )


async def find_last_bot_reply_time(session: AsyncSession, chat_id: str) -> int:
    """Return the latest assistant reply create_time (ms) in a chat, or 0."""
    result = await session.execute(
        select(func.max(ConversationMessage.create_time)).where(
            ConversationMessage.chat_id == chat_id,
            ConversationMessage.role == "assistant",
        )
    )
    return result.scalar_one_or_none() or 0


# --- Schedule ---


async def find_active_schedules_for_date(
    session: AsyncSession, now_date: str
) -> list[AkaoSchedule]:
    """Fetch all active schedule entries covering a given date.

    Returns raw entries — time-slot matching is the caller's responsibility.
    """
    result = await session.execute(
        select(AkaoSchedule)
        .where(AkaoSchedule.is_active.is_(True))
        .where(AkaoSchedule.period_start <= now_date)
        .where(AkaoSchedule.period_end >= now_date)
        .order_by(AkaoSchedule.plan_type.asc())
    )
    return list(result.scalars().all())


async def find_latest_plan(
    session: AsyncSession,
    plan_type: str,
    before_date: str,
    persona_id: str,
) -> AkaoSchedule | None:
    """Find the most recent plan of a type ending before a given date."""
    result = await session.execute(
        select(AkaoSchedule)
        .where(AkaoSchedule.plan_type == plan_type)
        .where(AkaoSchedule.is_active.is_(True))
        .where(AkaoSchedule.period_end < before_date)
        .where(AkaoSchedule.persona_id == persona_id)
        .order_by(AkaoSchedule.period_end.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def find_plan_for_period(
    session: AsyncSession,
    plan_type: str,
    period_start: str,
    period_end: str,
    persona_id: str,
) -> AkaoSchedule | None:
    """Look up a plan by exact period boundaries."""
    result = await session.execute(
        select(AkaoSchedule)
        .where(AkaoSchedule.plan_type == plan_type)
        .where(AkaoSchedule.period_start == period_start)
        .where(AkaoSchedule.period_end == period_end)
        .where(AkaoSchedule.persona_id == persona_id)
    )
    return result.scalar_one_or_none()


async def find_daily_entries(
    session: AsyncSession, target_date: str, persona_id: str
) -> list[AkaoSchedule]:
    """Fetch all daily time-slot entries for a given date."""
    result = await session.execute(
        select(AkaoSchedule)
        .where(AkaoSchedule.plan_type == "daily")
        .where(AkaoSchedule.period_start == target_date)
        .where(AkaoSchedule.is_active.is_(True))
        .where(AkaoSchedule.persona_id == persona_id)
        .order_by(AkaoSchedule.time_start.asc())
    )
    return list(result.scalars().all())


async def list_schedules(
    session: AsyncSession,
    *,
    plan_type: str | None = None,
    persona_id: str | None = None,
    active_only: bool = True,
    limit: int = 50,
) -> list[AkaoSchedule]:
    """List schedule entries with optional filters."""
    stmt = select(AkaoSchedule)
    if plan_type:
        stmt = stmt.where(AkaoSchedule.plan_type == plan_type)
    if persona_id:
        stmt = stmt.where(AkaoSchedule.persona_id == persona_id)
    if active_only:
        stmt = stmt.where(AkaoSchedule.is_active.is_(True))
    stmt = stmt.order_by(
        AkaoSchedule.period_start.desc(), AkaoSchedule.time_start.asc()
    ).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def upsert_schedule(session: AsyncSession, entry: AkaoSchedule) -> AkaoSchedule:
    """Insert or update a schedule entry (matched by unique constraint)."""
    result = await session.execute(
        select(AkaoSchedule)
        .where(AkaoSchedule.persona_id == entry.persona_id)
        .where(AkaoSchedule.plan_type == entry.plan_type)
        .where(AkaoSchedule.period_start == entry.period_start)
        .where(AkaoSchedule.period_end == entry.period_end)
        .where(
            AkaoSchedule.time_start == entry.time_start
            if entry.time_start
            else AkaoSchedule.time_start.is_(None)
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.content = entry.content
        existing.mood = entry.mood
        existing.energy_level = entry.energy_level
        existing.response_style_hint = entry.response_style_hint
        existing.proactive_action = entry.proactive_action
        existing.target_chats = entry.target_chats
        existing.model = entry.model
        existing.is_active = entry.is_active
        await session.flush()
        await session.refresh(existing)
        return existing

    session.add(entry)
    await session.flush()
    await session.refresh(entry)
    return entry


async def delete_schedule(session: AsyncSession, schedule_id: int) -> bool:
    """Delete a schedule entry by id. Returns True if found and deleted."""
    result = await session.execute(
        select(AkaoSchedule).where(AkaoSchedule.id == schedule_id)
    )
    entry = result.scalar_one_or_none()
    if not entry:
        return False
    await session.delete(entry)
    return True


# --- Life Engine ---


async def find_latest_life_state(
    session: AsyncSession, persona_id: str
) -> LifeEngineState | None:
    """Fetch the most recent life engine state row."""
    result = await session.execute(
        select(LifeEngineState)
        .where(LifeEngineState.persona_id == persona_id)
        .order_by(LifeEngineState.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def insert_life_state(
    session: AsyncSession,
    *,
    persona_id: str,
    current_state: str,
    activity_type: str,
    response_mood: str,
    skip_until: datetime | None,
    reasoning: str | None = None,
) -> None:
    """INSERT a new life engine state row (append-only)."""
    session.add(
        LifeEngineState(
            persona_id=persona_id,
            current_state=current_state,
            activity_type=activity_type,
            response_mood=response_mood,
            reasoning=reasoning,
            skip_until=skip_until,
        )
    )


async def find_today_activity_states(
    session: AsyncSession,
    persona_id: str,
    today_start: datetime,
) -> list[LifeEngineState]:
    """Fetch activity states created today (ascending)."""
    result = await session.execute(
        select(LifeEngineState)
        .where(LifeEngineState.persona_id == persona_id)
        .where(LifeEngineState.created_at >= today_start)
        .order_by(LifeEngineState.created_at.asc())
    )
    return list(result.scalars().all())


# --- Memory — fragments ---


async def insert_fragment(
    session: AsyncSession, fragment: ExperienceFragment
) -> ExperienceFragment:
    """Insert an experience fragment, return it with populated id."""
    session.add(fragment)
    await session.flush()
    await session.refresh(fragment)
    return fragment


async def find_recent_fragments_by_grain(
    session: AsyncSession,
    persona_id: str,
    grain: str,
    *,
    limit: int = 7,
) -> list[ExperienceFragment]:
    """Fetch recent fragments of a specific grain type (descending)."""
    result = await session.execute(
        select(ExperienceFragment)
        .where(ExperienceFragment.persona_id == persona_id)
        .where(ExperienceFragment.grain == grain)
        .order_by(ExperienceFragment.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def find_today_fragments(
    session: AsyncSession,
    persona_id: str,
    *,
    grains: list[str] | None = None,
    source_chat_id: str | None = None,
) -> list[ExperienceFragment]:
    """Fetch fragments created today (CST 00:00+, ascending)."""
    today_cst = datetime.now(_CST).replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = (
        select(ExperienceFragment)
        .where(ExperienceFragment.persona_id == persona_id)
        .where(ExperienceFragment.created_at >= today_cst)
    )
    if grains:
        stmt = stmt.where(ExperienceFragment.grain.in_(grains))
    if source_chat_id is not None:
        stmt = stmt.where(ExperienceFragment.source_chat_id == source_chat_id)
    stmt = stmt.order_by(ExperienceFragment.created_at.asc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def find_fragments_in_date_range(
    session: AsyncSession,
    persona_id: str,
    start_date: date,
    end_date: date,
    *,
    grains: list[str] | None = None,
) -> list[ExperienceFragment]:
    """Fetch fragments within a CST date range (inclusive, ascending)."""
    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=_CST)
    end_dt = datetime(
        end_date.year, end_date.month, end_date.day, tzinfo=_CST
    ) + timedelta(days=1)
    stmt = (
        select(ExperienceFragment)
        .where(ExperienceFragment.persona_id == persona_id)
        .where(ExperienceFragment.created_at >= start_dt)
        .where(ExperienceFragment.created_at < end_dt)
    )
    if grains:
        stmt = stmt.where(ExperienceFragment.grain.in_(grains))
    stmt = stmt.order_by(ExperienceFragment.created_at.asc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def search_fragments_fts(
    session: AsyncSession,
    persona_id: str,
    query: str,
    *,
    limit: int = 5,
) -> list[ExperienceFragment]:
    """Full-text search fragments using PostgreSQL simple dictionary."""
    result = await session.execute(
        select(ExperienceFragment)
        .where(ExperienceFragment.persona_id == persona_id)
        .where(
            text(
                "to_tsvector('simple', experience_fragment.content) "
                "@@ plainto_tsquery('simple', :query)"
            ).params(query=query)
        )
        .order_by(ExperienceFragment.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


# --- Memory — glimpse ---


async def find_latest_glimpse_state(
    session: AsyncSession,
    persona_id: str,
    chat_id: str,
) -> GlimpseState | None:
    """Fetch the most recent glimpse state for a persona+chat pair."""
    result = await session.execute(
        select(GlimpseState)
        .where(GlimpseState.persona_id == persona_id)
        .where(GlimpseState.chat_id == chat_id)
        .order_by(GlimpseState.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def insert_glimpse_state(
    session: AsyncSession,
    *,
    persona_id: str,
    chat_id: str,
    last_seen_msg_time: int,
    observation: str,
) -> None:
    """INSERT a new glimpse observation row (append-only)."""
    session.add(
        GlimpseState(
            persona_id=persona_id,
            chat_id=chat_id,
            last_seen_msg_time=last_seen_msg_time,
            observation=observation,
        )
    )


# --- Memory — reply style ---


async def insert_reply_style(
    session: AsyncSession,
    *,
    persona_id: str,
    style_text: str,
    source: str,
    observation: str | None = None,
) -> None:
    """Append a reply style audit log entry."""
    session.add(
        ReplyStyleLog(
            persona_id=persona_id,
            style_text=style_text,
            source=source,
            observation=observation,
        )
    )


async def find_latest_reply_style(session: AsyncSession, persona_id: str) -> str | None:
    """Fetch the most recent reply style text for a persona."""
    result = await session.execute(
        select(ReplyStyleLog.style_text)
        .where(ReplyStyleLog.persona_id == persona_id)
        .order_by(ReplyStyleLog.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# --- Memory — relationship ---


async def insert_relationship_memory(
    session: AsyncSession,
    *,
    persona_id: str,
    user_id: str,
    core_facts: str,
    impression: str,
    source: str,
) -> None:
    """Append a v2 relationship memory (auto-incrementing version)."""
    result = await session.execute(
        select(func.max(RelationshipMemoryV2.version))
        .where(RelationshipMemoryV2.persona_id == persona_id)
        .where(RelationshipMemoryV2.user_id == user_id)
    )
    max_version = result.scalar_one_or_none() or 0

    session.add(
        RelationshipMemoryV2(
            persona_id=persona_id,
            user_id=user_id,
            version=max_version + 1,
            core_facts=core_facts,
            impression=impression,
            source=source,
        )
    )


async def find_latest_relationship_memory(
    session: AsyncSession, persona_id: str, user_id: str
) -> tuple[str, str] | None:
    """Fetch the latest (core_facts, impression) for a user, or None."""
    result = await session.execute(
        select(
            RelationshipMemoryV2.core_facts,
            RelationshipMemoryV2.impression,
        )
        .where(RelationshipMemoryV2.persona_id == persona_id)
        .where(RelationshipMemoryV2.user_id == user_id)
        .order_by(RelationshipMemoryV2.version.desc())
        .limit(1)
    )
    row = result.one_or_none()
    if row is None:
        return None
    return (row.core_facts, row.impression)


async def find_relationship_memories_batch(
    session: AsyncSession,
    persona_id: str,
    user_ids: list[str],
) -> dict[str, tuple[str, str]]:
    """Batch-fetch latest relationship memories for multiple users."""
    if not user_ids:
        return {}

    result = await session.execute(
        select(
            RelationshipMemoryV2.user_id,
            RelationshipMemoryV2.core_facts,
            RelationshipMemoryV2.impression,
        )
        .where(RelationshipMemoryV2.persona_id == persona_id)
        .where(RelationshipMemoryV2.user_id.in_(user_ids))
        .distinct(RelationshipMemoryV2.user_id)
        .order_by(
            RelationshipMemoryV2.user_id,
            RelationshipMemoryV2.version.desc(),
        )
    )
    return {row.user_id: (row.core_facts, row.impression) for row in result.all()}


# --- Chat history — context messages and group members ---


async def find_context_messages_for_anchors(
    session: AsyncSession,
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
    result = await session.execute(stmt)
    return list(result.all())


async def find_group_members(
    session: AsyncSession,
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

    result = await session.execute(stmt)
    return list(result.all())
