"""记忆系统 v3 CRUD 操作

提供 ExperienceFragment 的增删改查。
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text as sql_text
from sqlalchemy.future import select

from .base import AsyncSessionLocal
from .memory_models import ExperienceFragment

# CST 时区
_CST = timezone(timedelta(hours=8))


async def create_fragment(fragment: ExperienceFragment) -> ExperienceFragment:
    """插入经历碎片，返回带 id 的对象"""
    async with AsyncSessionLocal() as session:
        session.add(fragment)
        await session.commit()
        await session.refresh(fragment)
        return fragment


async def get_fragments_for_chat(
    persona_id: str,
    source_chat_id: str,
    grains: list[str] | None = None,
    limit: int = 20,
) -> list[ExperienceFragment]:
    """按 chat + 可选粒度过滤，返回最近 N 条碎片（created_at DESC）"""
    async with AsyncSessionLocal() as session:
        stmt = (
            select(ExperienceFragment)
            .where(ExperienceFragment.persona_id == persona_id)
            .where(ExperienceFragment.source_chat_id == source_chat_id)
        )
        if grains:
            stmt = stmt.where(ExperienceFragment.grain.in_(grains))
        stmt = stmt.order_by(ExperienceFragment.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_recent_fragments_by_grain(
    persona_id: str,
    grain: str,
    limit: int = 7,
) -> list[ExperienceFragment]:
    """按粒度类型查最近 N 条碎片（created_at DESC）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ExperienceFragment)
            .where(ExperienceFragment.persona_id == persona_id)
            .where(ExperienceFragment.grain == grain)
            .order_by(ExperienceFragment.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def get_today_fragments(
    persona_id: str,
    grains: list[str] | None = None,
    source_chat_id: str | None = None,
) -> list[ExperienceFragment]:
    """查今日 CST 00:00 以后的碎片（created_at ASC）"""
    today_cst = datetime.now(_CST).replace(hour=0, minute=0, second=0, microsecond=0)
    async with AsyncSessionLocal() as session:
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


async def get_fragments_in_date_range(
    persona_id: str,
    start_date: date,
    end_date: date,
    grains: list[str] | None = None,
) -> list[ExperienceFragment]:
    """查指定日期范围内的碎片（以 CST 00:00 为边界，按时间正序）"""
    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=_CST)
    # end_date 含当天，取次日 00:00
    end_dt = datetime(end_date.year, end_date.month, end_date.day, tzinfo=_CST) + timedelta(days=1)
    async with AsyncSessionLocal() as session:
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
    persona_id: str,
    query: str,
    limit: int = 5,
) -> list[ExperienceFragment]:
    """PostgreSQL 全文搜索碎片内容（simple 词典，不分词，适合中文）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ExperienceFragment)
            .where(ExperienceFragment.persona_id == persona_id)
            .where(
                sql_text(
                    "to_tsvector('simple', experience_fragment.content) "
                    "@@ plainto_tsquery('simple', :query)"
                ).params(query=query)
            )
            .order_by(ExperienceFragment.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


