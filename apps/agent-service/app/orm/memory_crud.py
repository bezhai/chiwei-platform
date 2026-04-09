"""记忆系统 v3 CRUD 操作

提供 ExperienceFragment 的增删改查。
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text as sql_text
from sqlalchemy import func
from sqlalchemy.future import select

from .base import AsyncSessionLocal
from .memory_models import ExperienceFragment, GlimpseState

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


async def get_latest_glimpse_state(
    persona_id: str, chat_id: str
) -> GlimpseState | None:
    """查最新一行 glimpse 状态，不存在返回 None"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(GlimpseState)
            .where(GlimpseState.persona_id == persona_id)
            .where(GlimpseState.chat_id == chat_id)
            .order_by(GlimpseState.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def insert_glimpse_state(
    persona_id: str,
    chat_id: str,
    last_seen_msg_time: int,
    observation: str,
) -> None:
    """INSERT 一行新 glimpse 状态"""
    async with AsyncSessionLocal() as session:
        session.add(
            GlimpseState(
                persona_id=persona_id,
                chat_id=chat_id,
                last_seen_msg_time=last_seen_msg_time,
                observation=observation,
            )
        )
        await session.commit()


async def save_reply_style(
    persona_id: str,
    style_text: str,
    source: str,
    observation: str | None = None,
) -> None:
    """写入 reply_style 审计日志（append-only）"""
    from app.orm.memory_models import ReplyStyleLog

    async with AsyncSessionLocal() as session:
        session.add(ReplyStyleLog(
            persona_id=persona_id,
            style_text=style_text,
            source=source,
            observation=observation,
        ))
        await session.commit()


async def get_latest_reply_style(persona_id: str) -> str | None:
    """获取最新的 reply_style"""
    from app.orm.memory_models import ReplyStyleLog

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ReplyStyleLog.style_text)
            .where(ReplyStyleLog.persona_id == persona_id)
            .order_by(ReplyStyleLog.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return row


async def get_last_bot_reply_time(chat_id: str) -> int:
    """查指定群最近一次 assistant 回复的 create_time（毫秒），无则返回 0"""
    from sqlalchemy import func as sa_func

    from app.orm.models import ConversationMessage

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(sa_func.max(ConversationMessage.create_time)).where(
                ConversationMessage.chat_id == chat_id,
                ConversationMessage.role == "assistant",
            )
        )
        return result.scalar_one_or_none() or 0


async def save_relationship_memory(
    persona_id: str,
    user_id: str,
    user_name: str,
    core_facts: str,
    impression: str,
    source: str,
) -> None:
    """写入关系记忆（append-only，version 自增）"""
    from app.orm.memory_models import RelationshipMemory

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(func.max(RelationshipMemory.version))
            .where(RelationshipMemory.persona_id == persona_id)
            .where(RelationshipMemory.user_id == user_id)
        )
        max_version = result.scalar_one_or_none() or 0

        session.add(RelationshipMemory(
            persona_id=persona_id,
            user_id=user_id,
            user_name=user_name,
            memory_text="",
            version=max_version + 1,
            core_facts=core_facts,
            impression=impression,
            source=source,
        ))
        await session.commit()


async def get_latest_relationship_memory(
    persona_id: str, user_id: str
) -> tuple[str, str] | None:
    """获取指定用户的最新关系记忆，返回 (core_facts, impression) 或 None"""
    from app.orm.memory_models import RelationshipMemory

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                RelationshipMemory.core_facts,
                RelationshipMemory.impression,
                RelationshipMemory.memory_text,
            )
            .where(RelationshipMemory.persona_id == persona_id)
            .where(RelationshipMemory.user_id == user_id)
            .order_by(RelationshipMemory.created_at.desc())
            .limit(1)
        )
        row = result.one_or_none()
        if row is None:
            return None
        if row.core_facts or row.impression:
            return (row.core_facts, row.impression)
        return (row.memory_text, "")


async def get_relationship_memories_for_users(
    persona_id: str,
    user_ids: list[str],
) -> dict[str, tuple[str, str]]:
    """批量获取多个用户的最新关系记忆，返回 {user_id: (core_facts, impression)}"""
    from app.orm.memory_models import RelationshipMemory

    if not user_ids:
        return {}

    async with AsyncSessionLocal() as session:
        # 用 PostgreSQL DISTINCT ON 一次查出每个 user 的最新一行
        result = await session.execute(
            select(
                RelationshipMemory.user_id,
                RelationshipMemory.core_facts,
                RelationshipMemory.impression,
                RelationshipMemory.memory_text,
            )
            .where(RelationshipMemory.persona_id == persona_id)
            .where(RelationshipMemory.user_id.in_(user_ids))
            .distinct(RelationshipMemory.user_id)
            .order_by(RelationshipMemory.user_id, RelationshipMemory.created_at.desc())
        )
        out: dict[str, tuple[str, str]] = {}
        for row in result.all():
            if row.core_facts or row.impression:
                out[row.user_id] = (row.core_facts, row.impression)
            else:
                out[row.user_id] = (row.memory_text, "")
        return out

