"""Memory v4 fragment / abstract CRUD.

Operates on tables: ``Fragment``, ``AbstractMemory``, ``MemoryEdge``
(only for delete_fragment_query cascade).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.data.models import AbstractMemory, Fragment, MemoryEdge

__all__ = [
    "get_fragment_by_id",
    "get_abstract_by_id",
    "insert_fragment",
    "touch_fragment",
    "get_fragments_by_ids",
    "touch_fragments_bulk",
    "insert_abstract_memory",
    "touch_abstract",
    "touch_abstracts_bulk",
    "count_abstracts_by_persona",
    "update_abstract_content_query",
    "set_clarity",
    "delete_fragment_query",
]


async def get_fragment_by_id(
    session: AsyncSession, fragment_id: str
) -> Fragment | None:
    """Fetch a v4 Fragment by primary key."""
    result = await session.execute(select(Fragment).where(Fragment.id == fragment_id))
    return result.scalar_one_or_none()


async def get_abstract_by_id(
    session: AsyncSession, abstract_id: str
) -> AbstractMemory | None:
    """Fetch a v4 AbstractMemory by primary key."""
    result = await session.execute(
        select(AbstractMemory).where(AbstractMemory.id == abstract_id)
    )
    return result.scalar_one_or_none()


async def insert_fragment(
    session: AsyncSession,
    *,
    id: str,
    persona_id: str,
    content: str,
    source: str,
    chat_id: str | None = None,
    clarity: str = "clear",
    created_at: datetime | None = None,
) -> None:
    f = Fragment(
        id=id,
        persona_id=persona_id,
        content=content,
        source=source,
        chat_id=chat_id,
        clarity=clarity,
    )
    if created_at is not None:
        f.created_at = created_at
        f.last_touched_at = created_at
    session.add(f)


async def touch_fragment(session: AsyncSession, fragment_id: str) -> None:
    await session.execute(
        update(Fragment)
        .where(Fragment.id == fragment_id)
        .values(last_touched_at=func.now())
    )


async def get_fragments_by_ids(
    session: AsyncSession, ids: list[str]
) -> list[Fragment]:
    """Batch fetch fragments by id list. Preserves input order is NOT guaranteed."""
    if not ids:
        return []
    result = await session.execute(
        select(Fragment).where(Fragment.id.in_(ids))
    )
    return list(result.scalars().all())


async def touch_fragments_bulk(session: AsyncSession, ids: list[str]) -> None:
    """Update last_touched_at=NOW() for many fragments at once."""
    if not ids:
        return
    await session.execute(
        update(Fragment)
        .where(Fragment.id.in_(ids))
        .values(last_touched_at=func.now())
    )


async def insert_abstract_memory(
    session: AsyncSession,
    *,
    id: str,
    persona_id: str,
    subject: str,
    content: str,
    created_by: str,
    clarity: str = "clear",
) -> None:
    a = AbstractMemory(
        id=id,
        persona_id=persona_id,
        subject=subject,
        content=content,
        created_by=created_by,
        clarity=clarity,
    )
    session.add(a)


async def touch_abstract(session: AsyncSession, abstract_id: str) -> None:
    await session.execute(
        update(AbstractMemory)
        .where(AbstractMemory.id == abstract_id)
        .values(last_touched_at=func.now())
    )


async def touch_abstracts_bulk(session: AsyncSession, ids: list[str]) -> None:
    """Update last_touched_at=NOW() for many abstracts at once."""
    if not ids:
        return
    await session.execute(
        update(AbstractMemory)
        .where(AbstractMemory.id.in_(ids))
        .values(last_touched_at=func.now())
    )


async def count_abstracts_by_persona(
    session: AsyncSession, persona_id: str
) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(AbstractMemory)
        .where(AbstractMemory.persona_id == persona_id)
    )
    return int(result.scalar_one())


async def update_abstract_content_query(
    session: AsyncSession, *, abstract_id: str, new_content: str
) -> None:
    await session.execute(
        update(AbstractMemory)
        .where(AbstractMemory.id == abstract_id)
        .values(content=new_content, last_touched_at=func.now())
    )


async def set_clarity(
    session: AsyncSession, *, node_id: str, node_type: str, clarity: str
) -> None:
    if node_type == "abstract":
        await session.execute(
            update(AbstractMemory)
            .where(AbstractMemory.id == node_id)
            .values(clarity=clarity, last_touched_at=func.now())
        )
    elif node_type == "fact":
        await session.execute(
            update(Fragment)
            .where(Fragment.id == node_id)
            .values(clarity=clarity, last_touched_at=func.now())
        )
    else:
        raise ValueError(f"unknown node_type {node_type}")


async def delete_fragment_query(
    session: AsyncSession, *, fragment_id: str
) -> None:
    # cascade delete edges touching this fragment first
    await session.execute(
        MemoryEdge.__table__.delete().where(
            or_(MemoryEdge.from_id == fragment_id, MemoryEdge.to_id == fragment_id)
        )
    )
    await session.execute(
        Fragment.__table__.delete().where(Fragment.id == fragment_id)
    )
