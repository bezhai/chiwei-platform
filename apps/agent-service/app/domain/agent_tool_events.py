"""Agent tool side-effect events.

Each mutation tool (commit_abstract / update_schedule / notes) writes DB
then emits one of these Data classes; downstream nodes react via wire
(vectorize / state-sync / reviewer / etc.). Tools no longer call
ad-hoc bypass helpers (mq.publish / arq enqueue / direct pool access).

All transient — these are events, not durable rows; the underlying DB
row (AbstractMemory / ScheduleRevision / Note) is the source of truth.
"""
from __future__ import annotations

from typing import Annotated

from app.runtime import Data, Key


class AbstractMemoryCommitted(Data):
    """commit_abstract tool wrote an abstract memory + edges."""
    abstract_id: Annotated[str, Key]
    persona_id: str
    chat_id: str | None = None

    class Meta:
        transient = True


class ScheduleRevisionCreated(Data):
    """update_schedule tool wrote a schedule_revision row."""
    revision_id: Annotated[str, Key]
    persona_id: str

    class Meta:
        transient = True


class NoteCreated(Data):
    """notes tool created a new note."""
    note_id: Annotated[str, Key]
    persona_id: str

    class Meta:
        transient = True
