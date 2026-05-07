"""Memory v4 edges + notes — graph traversal helpers and Note CRUD.

Operates on tables: ``MemoryEdge``, ``Note``.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.data.models import MemoryEdge, Note

__all__ = [
    "insert_memory_edge",
    "delete_edge",
    "list_edges_to",
    "list_edges_from",
    "insert_note",
    "get_active_notes",
    "resolve_note",
]


async def insert_memory_edge(
    session: AsyncSession,
    *,
    id: str,
    persona_id: str,
    from_id: str,
    from_type: str,
    to_id: str,
    to_type: str,
    edge_type: str,
    created_by: str,
    reason: str | None = None,
) -> None:
    e = MemoryEdge(
        id=id,
        persona_id=persona_id,
        from_id=from_id,
        from_type=from_type,
        to_id=to_id,
        to_type=to_type,
        edge_type=edge_type,
        created_by=created_by,
        reason=reason,
    )
    session.add(e)


async def delete_edge(
    session: AsyncSession, *, edge_id: str
) -> None:
    await session.execute(
        MemoryEdge.__table__.delete().where(MemoryEdge.id == edge_id)
    )


async def list_edges_to(
    session: AsyncSession,
    *,
    persona_id: str,
    to_id: str,
    edge_type: str | None = None,
) -> list[MemoryEdge]:
    """List edges whose ``to_id`` matches, optionally filtered by edge_type."""
    stmt = (
        select(MemoryEdge)
        .where(MemoryEdge.persona_id == persona_id)
        .where(MemoryEdge.to_id == to_id)
    )
    if edge_type:
        stmt = stmt.where(MemoryEdge.edge_type == edge_type)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_edges_from(
    session: AsyncSession,
    *,
    persona_id: str,
    from_id: str,
    edge_type: str | None = None,
) -> list[MemoryEdge]:
    """List edges whose ``from_id`` matches, optionally filtered by edge_type."""
    stmt = (
        select(MemoryEdge)
        .where(MemoryEdge.persona_id == persona_id)
        .where(MemoryEdge.from_id == from_id)
    )
    if edge_type:
        stmt = stmt.where(MemoryEdge.edge_type == edge_type)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def insert_note(
    session: AsyncSession,
    *,
    id: str,
    persona_id: str,
    content: str,
    when_at: datetime | None = None,
) -> None:
    n = Note(id=id, persona_id=persona_id, content=content, when_at=when_at)
    session.add(n)


async def get_active_notes(
    session: AsyncSession, persona_id: str
) -> list[Note]:
    result = await session.execute(
        select(Note)
        .where(Note.persona_id == persona_id)
        .where(Note.resolved_at.is_(None))
        .order_by(Note.created_at.desc())
    )
    return list(result.scalars().all())


async def resolve_note(
    session: AsyncSession, *, note_id: str, resolution: str
) -> None:
    await session.execute(
        update(Note)
        .where(Note.id == note_id)
        .values(resolved_at=func.now(), resolution=resolution)
    )
