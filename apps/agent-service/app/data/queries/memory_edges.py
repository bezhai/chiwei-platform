"""Memory v4 edges + notes — graph traversal helpers and Note CRUD.

Operates on tables: ``MemoryEdge``, ``Note``.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, update
from sqlalchemy.future import select

from app.data.models import MemoryEdge, Note
from app.runtime.db import auto_tx, current_session

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
    async with auto_tx():
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
        current_session().add(e)


async def delete_edge(*, edge_id: str) -> None:
    async with auto_tx():
        await current_session().execute(
            MemoryEdge.__table__.delete().where(MemoryEdge.id == edge_id)
        )


async def list_edges_to(
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
    async with auto_tx():
        result = await current_session().execute(stmt)
        return list(result.scalars().all())


async def list_edges_from(
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
    async with auto_tx():
        result = await current_session().execute(stmt)
        return list(result.scalars().all())


async def insert_note(
    *,
    id: str,
    persona_id: str,
    content: str,
    when_at: datetime | None = None,
) -> None:
    async with auto_tx():
        s = current_session()
        n = Note(id=id, persona_id=persona_id, content=content, when_at=when_at)
        s.add(n)
        await s.flush()


async def get_active_notes(persona_id: str) -> list[Note]:
    async with auto_tx():
        result = await current_session().execute(
            select(Note)
            .where(Note.persona_id == persona_id)
            .where(Note.resolved_at.is_(None))
            .order_by(Note.created_at.desc())
        )
        return list(result.scalars().all())


async def resolve_note(*, note_id: str, resolution: str) -> None:
    async with auto_tx():
        await current_session().execute(
            update(Note)
            .where(Note.id == note_id)
            .values(resolved_at=func.now(), resolution=resolution)
        )
