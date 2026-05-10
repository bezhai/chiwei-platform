"""Memory v4 edges + notes — graph traversal helpers and Note CRUD.

Operates on tables: ``MemoryEdge``, ``Note``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, update
from sqlalchemy.future import select

from app.data.ids import new_id
from app.data.models import MemoryEdge, Note
from app.runtime.db import auto_tx, current_session

__all__ = [
    "insert_memory_edge",
    "delete_edge",
    "list_edges_to",
    "list_edges_from",
    "upsert_note",
    "delete_note",
    "list_active_notes",
    "select_notes_for_context",
    "resolve_note",
]


_UNSET: object = object()


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


async def upsert_note(
    *,
    persona_id: str,
    content: str,
    when_at: datetime | None | object = _UNSET,
    note_id: str | None = None,
) -> Note:
    """Create or update a Note.

    - ``note_id is None`` → create new note (id auto-generated as ``n_<hex>``)
    - ``note_id`` provided + ``when_at is _UNSET`` → update content only
    - ``note_id`` provided + ``when_at is None`` → clear when_at column
    - ``note_id`` provided + ``when_at`` is datetime → update when_at column

    Returns the persisted Note. Raises ``LookupError`` if updating an unknown id.
    """
    async with auto_tx():
        s = current_session()
        if note_id is None:
            nid = new_id("n")
            when_value = None if when_at is _UNSET else when_at
            n = Note(
                id=nid,
                persona_id=persona_id,
                content=content,
                when_at=when_value,
            )
            s.add(n)
            await s.flush()
            return n

        result = await s.execute(select(Note).where(Note.id == note_id))
        existing = result.scalar_one_or_none()
        if existing is None:
            raise LookupError(f"note not found: {note_id}")

        existing.content = content
        if when_at is not _UNSET:
            existing.when_at = when_at  # type: ignore[assignment]
        await s.flush()
        return existing


async def list_active_notes(persona_id: str) -> list[Note]:
    """Return all notes that are neither resolved nor deleted.

    Ordered: notes with ``when_at`` first (ascending — soonest first),
    then notes without ``when_at`` (most recently created first).
    """
    async with auto_tx():
        result = await current_session().execute(
            select(Note)
            .where(Note.persona_id == persona_id)
            .where(Note.resolved_at.is_(None))
            .where(Note.deleted_at.is_(None))
            .order_by(
                Note.when_at.asc().nulls_last(),
                Note.created_at.desc(),
            )
        )
        return list(result.scalars().all())


_CONTEXT_RECENT_OVERDUE_DAYS = 3
_CONTEXT_NEW_MEMO_DAYS = 7
_CONTEXT_NOTES_LIMIT = 15


async def select_notes_for_context(persona_id: str) -> list[Note]:
    """Return notes appropriate for live context injection.

    Filters:
    - Notes with ``when_at`` are kept if ``when_at >= now - 3 days``
      (upcoming + recently overdue).
    - Notes without ``when_at`` are kept if ``created_at >= now - 7 days``
      (fresh memos).

    Order: notes with ``when_at`` first (ascending — soonest first), then
    notes without ``when_at`` (most recent created first). Capped at 15 rows.
    """
    now = datetime.now(timezone.utc)
    overdue_cutoff = now - timedelta(days=_CONTEXT_RECENT_OVERDUE_DAYS)
    memo_cutoff = now - timedelta(days=_CONTEXT_NEW_MEMO_DAYS)

    async with auto_tx():
        result = await current_session().execute(
            select(Note)
            .where(Note.persona_id == persona_id)
            .where(Note.resolved_at.is_(None))
            .where(Note.deleted_at.is_(None))
            .where(
                (Note.when_at.is_not(None) & (Note.when_at >= overdue_cutoff))
                | (Note.when_at.is_(None) & (Note.created_at >= memo_cutoff))
            )
            .order_by(
                Note.when_at.asc().nulls_last(),
                Note.created_at.desc(),
            )
            .limit(_CONTEXT_NOTES_LIMIT)
        )
        return list(result.scalars().all())


async def delete_note(*, note_id: str, reason: str) -> None:
    """Soft-delete a note (sets deleted_at + delete_reason).

    Does not raise if note_id does not exist; the UPDATE simply affects 0 rows.
    The tool layer is responsible for verifying existence if needed.
    """
    async with auto_tx():
        await current_session().execute(
            update(Note)
            .where(Note.id == note_id)
            .values(deleted_at=func.now(), delete_reason=reason)
        )


async def resolve_note(*, note_id: str, resolution: str) -> None:
    async with auto_tx():
        await current_session().execute(
            update(Note)
            .where(Note.id == note_id)
            .values(resolved_at=func.now(), resolution=resolution)
        )
