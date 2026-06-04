"""Agent tool side-effect events.

Each mutation tool (commit_abstract / notes) writes DB then emits one of
these Data classes; downstream nodes react via wire (vectorize / reviewer /
etc.). Tools no longer call ad-hoc bypass helpers (mq.publish / arq enqueue /
direct pool access).

AbstractMemoryCommitted / NoteCreated stay transient — their downstream
edges are in-process re-emits, the underlying DB row (AbstractMemory /
Note) is the source of truth.
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


class NoteCreated(Data):
    """notes tool created a new note."""
    note_id: Annotated[str, Key]
    persona_id: str

    class Meta:
        transient = True
