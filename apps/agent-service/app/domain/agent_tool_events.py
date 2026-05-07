"""Agent tool side-effect events.

Each mutation tool (commit_abstract / update_schedule / notes) writes DB
then emits one of these Data classes; downstream nodes react via wire
(vectorize / state-sync / reviewer / etc.). Tools no longer call
ad-hoc bypass helpers (mq.publish / arq enqueue / direct pool access).

AbstractMemoryCommitted / NoteCreated stay transient — their downstream
edges are in-process re-emits, the underlying DB row (AbstractMemory /
Note) is the source of truth. ScheduleRevisionCreated is persisted
because its wire is .durable() (cross-process tool -> sync_life_state
consumer), and durable edges require a real pg table for
``insert_idempotent`` mq-redelivery dedup.
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
    """update_schedule tool wrote a schedule_revision row.

    Persisted (NOT transient) — wire(...).durable() requires a real pg
    table so the consumer-side ``insert_idempotent`` can dedup mq
    redeliveries. The revision_id key is unique per real DB row in
    schedule_revisions, so this Data row is also the durable event log.
    """
    revision_id: Annotated[str, Key]
    persona_id: str


class NoteCreated(Data):
    """notes tool created a new note."""
    note_id: Annotated[str, Key]
    persona_id: str

    class Meta:
        transient = True
