"""Life-engine queries — life_engine_state, glimpse_state, reply_style_log.

Operates on tables: ``LifeEngineState``, ``GlimpseState``, ``ReplyStyleLog``.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.future import select

from app.data.models import GlimpseState, LifeEngineState, ReplyStyleLog
from app.runtime.db import auto_tx, current_session

__all__ = [
    "find_latest_life_state",
    "insert_life_state",
    "find_today_activity_states",
    "find_life_states_in_range",
    "find_latest_glimpse_state",
    "insert_glimpse_state",
    "insert_reply_style",
    "find_latest_reply_style",
    "list_recent_life_states",
]


async def find_latest_life_state(persona_id: str) -> LifeEngineState | None:
    """Fetch the most recent life engine state row."""
    async with auto_tx():
        result = await current_session().execute(
            select(LifeEngineState)
            .where(LifeEngineState.persona_id == persona_id)
            .order_by(LifeEngineState.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def insert_life_state(
    *,
    persona_id: str,
    current_state: str,
    activity_type: str,
    response_mood: str,
    skip_until: datetime | None,
    reasoning: str | None = None,
    state_end_at: datetime | None = None,
) -> int:
    """INSERT a new life engine state row (append-only). Returns the new row id."""
    async with auto_tx():
        s = current_session()
        row = LifeEngineState(
            persona_id=persona_id,
            current_state=current_state,
            activity_type=activity_type,
            response_mood=response_mood,
            reasoning=reasoning,
            skip_until=skip_until,
            state_end_at=state_end_at,
        )
        s.add(row)
        await s.flush()
        return row.id


async def find_today_activity_states(
    persona_id: str,
    today_start: datetime,
) -> list[LifeEngineState]:
    """Fetch activity states created today (ascending)."""
    async with auto_tx():
        result = await current_session().execute(
            select(LifeEngineState)
            .where(LifeEngineState.persona_id == persona_id)
            .where(LifeEngineState.created_at >= today_start)
            .order_by(LifeEngineState.created_at.asc())
        )
        return list(result.scalars().all())


async def find_life_states_in_range(
    persona_id: str,
    start_dt: datetime,
    end_dt: datetime,
) -> list[LifeEngineState]:
    """Fetch life_engine_state rows within a datetime range (ascending)."""
    async with auto_tx():
        result = await current_session().execute(
            select(LifeEngineState)
            .where(LifeEngineState.persona_id == persona_id)
            .where(LifeEngineState.created_at >= start_dt)
            .where(LifeEngineState.created_at < end_dt)
            .order_by(LifeEngineState.created_at.asc())
        )
        return list(result.scalars().all())


async def find_latest_glimpse_state(
    persona_id: str,
    chat_id: str,
) -> GlimpseState | None:
    """Fetch the most recent glimpse state for a persona+chat pair."""
    async with auto_tx():
        result = await current_session().execute(
            select(GlimpseState)
            .where(GlimpseState.persona_id == persona_id)
            .where(GlimpseState.chat_id == chat_id)
            .order_by(GlimpseState.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def insert_glimpse_state(
    *,
    persona_id: str,
    chat_id: str,
    last_seen_msg_time: int,
    observation: str,
) -> None:
    """INSERT a new glimpse observation row (append-only)."""
    async with auto_tx():
        current_session().add(
            GlimpseState(
                persona_id=persona_id,
                chat_id=chat_id,
                last_seen_msg_time=last_seen_msg_time,
                observation=observation,
            )
        )


async def insert_reply_style(
    *,
    persona_id: str,
    style_text: str,
    source: str,
    observation: str | None = None,
) -> None:
    """Append a reply style audit log entry."""
    async with auto_tx():
        current_session().add(
            ReplyStyleLog(
                persona_id=persona_id,
                style_text=style_text,
                source=source,
                observation=observation,
            )
        )


async def find_latest_reply_style(persona_id: str) -> str | None:
    """Fetch the most recent reply style text for a persona."""
    async with auto_tx():
        result = await current_session().execute(
            select(ReplyStyleLog.style_text)
            .where(ReplyStyleLog.persona_id == persona_id)
            .order_by(ReplyStyleLog.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def list_recent_life_states(
    *, persona_id: str, since: datetime
) -> list[LifeEngineState]:
    async with auto_tx():
        result = await current_session().execute(
            select(LifeEngineState)
            .where(LifeEngineState.persona_id == persona_id)
            .where(LifeEngineState.created_at >= since)
            .order_by(LifeEngineState.created_at)
        )
        return list(result.scalars().all())
