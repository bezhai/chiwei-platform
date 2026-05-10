"""Schedule queries — AkaoSchedule (legacy) + ScheduleRevision (life-engine).

Operates on tables: ``AkaoSchedule``, ``ScheduleRevision``.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.future import select

from app.data.models import AkaoSchedule, ScheduleRevision
from app.runtime.db import auto_tx, current_session

__all__ = [
    "find_active_schedules_for_date",
    "find_latest_plan",
    "find_plan_for_period",
    "find_daily_entries",
    "list_schedules",
    "upsert_schedule",
    "delete_schedule",
    "insert_schedule_revision",
    "get_current_schedule",
    "get_schedule_revision_by_id",
    "list_recent_schedule_revisions",
]


async def find_active_schedules_for_date(now_date: str) -> list[AkaoSchedule]:
    """Fetch all active schedule entries covering a given date.

    Returns raw entries — time-slot matching is the caller's responsibility.
    """
    async with auto_tx():
        result = await current_session().execute(
            select(AkaoSchedule)
            .where(AkaoSchedule.is_active.is_(True))
            .where(AkaoSchedule.period_start <= now_date)
            .where(AkaoSchedule.period_end >= now_date)
            .order_by(AkaoSchedule.plan_type.asc())
        )
        return list(result.scalars().all())


async def find_latest_plan(
    plan_type: str,
    before_date: str,
    persona_id: str,
) -> AkaoSchedule | None:
    """Find the most recent plan of a type ending before a given date."""
    async with auto_tx():
        result = await current_session().execute(
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
    plan_type: str,
    period_start: str,
    period_end: str,
    persona_id: str,
) -> AkaoSchedule | None:
    """Look up a plan by exact period boundaries."""
    async with auto_tx():
        result = await current_session().execute(
            select(AkaoSchedule)
            .where(AkaoSchedule.plan_type == plan_type)
            .where(AkaoSchedule.period_start == period_start)
            .where(AkaoSchedule.period_end == period_end)
            .where(AkaoSchedule.persona_id == persona_id)
        )
        return result.scalar_one_or_none()


async def find_daily_entries(
    target_date: str, persona_id: str
) -> list[AkaoSchedule]:
    """Fetch all daily time-slot entries for a given date."""
    async with auto_tx():
        result = await current_session().execute(
            select(AkaoSchedule)
            .where(AkaoSchedule.plan_type == "daily")
            .where(AkaoSchedule.period_start == target_date)
            .where(AkaoSchedule.is_active.is_(True))
            .where(AkaoSchedule.persona_id == persona_id)
            .order_by(AkaoSchedule.time_start.asc())
        )
        return list(result.scalars().all())


async def list_schedules(
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
    async with auto_tx():
        result = await current_session().execute(stmt)
        return list(result.scalars().all())


async def upsert_schedule(entry: AkaoSchedule) -> AkaoSchedule:
    """Insert or update a schedule entry (matched by unique constraint)."""
    async with auto_tx():
        s = current_session()
        result = await s.execute(
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
            await s.flush()
            await s.refresh(existing)
            return existing

        s.add(entry)
        await s.flush()
        await s.refresh(entry)
        return entry


async def delete_schedule(schedule_id: int) -> bool:
    """Delete a schedule entry by id. Returns True if found and deleted."""
    async with auto_tx():
        s = current_session()
        result = await s.execute(
            select(AkaoSchedule).where(AkaoSchedule.id == schedule_id)
        )
        entry = result.scalar_one_or_none()
        if not entry:
            return False
        await s.delete(entry)
        return True


async def insert_schedule_revision(
    *,
    id: str,
    persona_id: str,
    content: str,
    reason: str,
    created_by: str,
) -> None:
    async with auto_tx():
        sr = ScheduleRevision(
            id=id,
            persona_id=persona_id,
            content=content,
            reason=reason,
            created_by=created_by,
        )
        current_session().add(sr)


async def get_current_schedule(persona_id: str) -> ScheduleRevision | None:
    async with auto_tx():
        result = await current_session().execute(
            select(ScheduleRevision)
            .where(ScheduleRevision.persona_id == persona_id)
            .order_by(ScheduleRevision.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def get_schedule_revision_by_id(revision_id: str) -> ScheduleRevision | None:
    """Fetch a schedule_revision by id."""
    async with auto_tx():
        result = await current_session().execute(
            select(ScheduleRevision).where(ScheduleRevision.id == revision_id)
        )
        return result.scalar_one_or_none()


async def list_recent_schedule_revisions(
    *, persona_id: str, since: datetime
) -> list[ScheduleRevision]:
    async with auto_tx():
        result = await current_session().execute(
            select(ScheduleRevision)
            .where(ScheduleRevision.persona_id == persona_id)
            .where(ScheduleRevision.created_at >= since)
            .order_by(ScheduleRevision.created_at)
        )
        return list(result.scalars().all())
