"""Memory v4 search / window queries — read helpers for fragments + abstracts.

Operates on tables: ``Fragment``, ``AbstractMemory``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.future import select

from app.data.models import AbstractMemory, Fragment
from app.runtime.db import auto_tx, current_session

# CST timezone for date boundary calculations (CST 00:00 day boundary).
_CST = timezone(timedelta(hours=8))

__all__ = [
    "list_today_fragments",
    "find_fragments_since",
    "list_fragments_window",
    "list_abstracts_window",
    "get_abstracts_by_subject",
    "get_abstracts_by_subjects",
    "get_recent_abstract_titles",
    "count_abstracts_per_subject_prefix",
    "get_recent_fragments_for_injection",
]


async def list_today_fragments(
    persona_id: str,
    *,
    sources: list[str] | None = None,
) -> list[Fragment]:
    """Fetch v4 Fragments created today (CST 00:00+, ascending). Skips forgotten rows."""
    today_cst = datetime.now(_CST).replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = (
        select(Fragment)
        .where(Fragment.persona_id == persona_id)
        .where(Fragment.created_at >= today_cst)
        .where(Fragment.clarity != "forgotten")
    )
    if sources:
        stmt = stmt.where(Fragment.source.in_(sources))
    stmt = stmt.order_by(Fragment.created_at.asc())
    async with auto_tx():
        result = await current_session().execute(stmt)
        return list(result.scalars().all())


async def find_fragments_since(
    persona_id: str,
    since_dt: datetime,
    *,
    sources: list[str] | None = None,
    limit: int = 50,
) -> list[Fragment]:
    """Fetch v4 Fragments created since a given datetime (descending). Skips forgotten rows."""
    stmt = (
        select(Fragment)
        .where(Fragment.persona_id == persona_id)
        .where(Fragment.created_at >= since_dt)
        .where(Fragment.clarity != "forgotten")
    )
    if sources:
        stmt = stmt.where(Fragment.source.in_(sources))
    stmt = stmt.order_by(Fragment.created_at.desc()).limit(limit)
    async with auto_tx():
        result = await current_session().execute(stmt)
        return list(result.scalars().all())


async def list_fragments_window(
    *, persona_id: str, since: datetime,
) -> list[Fragment]:
    async with auto_tx():
        result = await current_session().execute(
            select(Fragment)
            .where(Fragment.persona_id == persona_id)
            .where(Fragment.created_at >= since)
            .where(Fragment.clarity != "forgotten")
            .order_by(Fragment.created_at)
        )
        return list(result.scalars().all())


async def list_abstracts_window(
    *, persona_id: str, since: datetime,
) -> list[AbstractMemory]:
    async with auto_tx():
        result = await current_session().execute(
            select(AbstractMemory)
            .where(AbstractMemory.persona_id == persona_id)
            .where(AbstractMemory.created_at >= since)
            .where(AbstractMemory.clarity != "forgotten")
            .order_by(AbstractMemory.created_at)
        )
        return list(result.scalars().all())


async def get_abstracts_by_subject(
    *,
    persona_id: str,
    subject: str,
    limit: int = 20,
) -> list[AbstractMemory]:
    """Fetch non-forgotten abstracts for a subject, newest-touched first."""
    async with auto_tx():
        result = await current_session().execute(
            select(AbstractMemory)
            .where(AbstractMemory.persona_id == persona_id)
            .where(AbstractMemory.subject == subject)
            .where(AbstractMemory.clarity != "forgotten")
            .order_by(AbstractMemory.last_touched_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def get_abstracts_by_subjects(
    *,
    persona_id: str,
    subjects: list[str],
    limit_per_subject: int = 5,
) -> list[AbstractMemory]:
    """Get abstracts whose subject is in given list (for always-on injection)."""
    if not subjects:
        return []
    async with auto_tx():
        result = await current_session().execute(
            select(AbstractMemory)
            .where(AbstractMemory.persona_id == persona_id)
            .where(AbstractMemory.subject.in_(subjects))
            .where(AbstractMemory.clarity != "forgotten")
            .order_by(
                AbstractMemory.subject,
                AbstractMemory.last_touched_at.desc(),
            )
        )
        rows = list(result.scalars().all())
    # Keep at most `limit_per_subject` per subject
    by_subject: dict[str, list[AbstractMemory]] = {}
    for r in rows:
        by_subject.setdefault(r.subject, []).append(r)
    out: list[AbstractMemory] = []
    for subj in subjects:
        out.extend(by_subject.get(subj, [])[:limit_per_subject])
    return out


async def get_recent_abstract_titles(
    *,
    persona_id: str,
    limit: int = 10,
) -> list[AbstractMemory]:
    """Recently touched abstracts — for recall-index hint."""
    async with auto_tx():
        result = await current_session().execute(
            select(AbstractMemory)
            .where(AbstractMemory.persona_id == persona_id)
            .where(AbstractMemory.clarity != "forgotten")
            .order_by(AbstractMemory.last_touched_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def count_abstracts_per_subject_prefix(
    *,
    persona_id: str,
    prefix: str,
) -> int:
    """Count non-forgotten abstracts whose subject starts with prefix."""
    async with auto_tx():
        result = await current_session().execute(
            select(func.count())
            .select_from(AbstractMemory)
            .where(AbstractMemory.persona_id == persona_id)
            .where(AbstractMemory.subject.like(f"{prefix}%"))
            .where(AbstractMemory.clarity != "forgotten")
        )
        return int(result.scalar_one())


async def get_recent_fragments_for_injection(
    *,
    persona_id: str,
    chat_id: str | None,
    _trigger_user_id: str | None,
    max_same_chat: int = 1,
    max_other_chat: int = 2,
    hours: int = 4,
) -> list[Fragment]:
    """短期注入规则：
    - 当前 chat 最近 N 小时内的最新 1 条 fragment
    - 其他 chat 最近 N 小时内的 fragment（最多 max_other_chat 条，每 chat 只取最新）

    ``_trigger_user_id``: reserved for future user-scoped filtering; currently ignored.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    stmt = (
        select(Fragment)
        .where(Fragment.persona_id == persona_id)
        .where(Fragment.clarity != "forgotten")
        .where(Fragment.created_at >= since)
        .order_by(Fragment.created_at.desc())
        .limit(max_same_chat + max_other_chat * 20)
    )
    async with auto_tx():
        result = await current_session().execute(stmt)
        all_recent = list(result.scalars().all())

    same_chat: list[Fragment] = []
    other_chats: dict[str, Fragment] = {}
    for f in all_recent:
        if chat_id and f.chat_id == chat_id:
            if len(same_chat) < max_same_chat:
                same_chat.append(f)
        elif f.chat_id and f.chat_id not in other_chats:
            other_chats[f.chat_id] = f

    other_list = list(other_chats.values())[:max_other_chat]
    return same_chat + other_list
