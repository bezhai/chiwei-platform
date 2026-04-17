"""Test v4 basic CRUD queries — unit tests with mocked session."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.data.models import AbstractMemory, Fragment, MemoryEdge, Note
from app.data.queries import (
    count_abstracts_by_persona,
    get_active_notes,
    get_current_schedule,
    insert_abstract_memory,
    insert_fragment,
    insert_memory_edge,
    insert_note,
    insert_schedule_revision,
    resolve_note,
    touch_abstract,
    touch_fragment,
)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class _IterResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return self._values


@pytest.mark.asyncio
async def test_insert_fragment_adds_fragment_to_session():
    session = AsyncMock()
    session.add = lambda obj: setattr(session, "_added", obj)

    await insert_fragment(
        session,
        id="f1",
        persona_id="chiwei",
        content="hello",
        source="manual",
        chat_id="chat1",
    )
    added = session._added
    assert isinstance(added, Fragment)
    assert added.id == "f1"
    assert added.content == "hello"
    assert added.chat_id == "chat1"


@pytest.mark.asyncio
async def test_touch_fragment_executes_update():
    session = AsyncMock()
    session.execute = AsyncMock()
    await touch_fragment(session, "f1")
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_insert_abstract_memory_adds_to_session():
    session = AsyncMock()
    session.add = lambda obj: setattr(session, "_added", obj)

    await insert_abstract_memory(
        session,
        id="a1",
        persona_id="chiwei",
        subject="user:u1",
        content="abstract",
        created_by="chiwei",
    )
    added = session._added
    assert isinstance(added, AbstractMemory)
    assert added.subject == "user:u1"


@pytest.mark.asyncio
async def test_touch_abstract_executes_update():
    session = AsyncMock()
    session.execute = AsyncMock()
    await touch_abstract(session, "a1")
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_count_abstracts_by_persona_returns_int():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(5))
    cnt = await count_abstracts_by_persona(session, persona_id="chiwei")
    assert cnt == 5


@pytest.mark.asyncio
async def test_insert_memory_edge_adds_to_session():
    session = AsyncMock()
    session.add = lambda obj: setattr(session, "_added", obj)

    await insert_memory_edge(
        session,
        id="e1",
        persona_id="chiwei",
        from_id="f1",
        from_type="fact",
        to_id="a1",
        to_type="abstract",
        edge_type="supports",
        created_by="chiwei",
        reason="test",
    )
    added = session._added
    assert isinstance(added, MemoryEdge)
    assert added.edge_type == "supports"
    assert added.reason == "test"


@pytest.mark.asyncio
async def test_insert_note_adds_to_session():
    session = AsyncMock()
    session.add = lambda obj: setattr(session, "_added", obj)

    await insert_note(
        session,
        id="n1",
        persona_id="chiwei",
        content="周五看电影",
    )
    added = session._added
    assert isinstance(added, Note)
    assert added.content == "周五看电影"
    assert added.when_at is None


@pytest.mark.asyncio
async def test_get_active_notes_returns_list():
    note = Note(id="n1", persona_id="chiwei", content="x")
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([note]))

    result = await get_active_notes(session, persona_id="chiwei")
    assert result == [note]


@pytest.mark.asyncio
async def test_resolve_note_executes_update():
    session = AsyncMock()
    session.execute = AsyncMock()
    await resolve_note(session, note_id="n1", resolution="done")
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_insert_schedule_revision_adds_to_session():
    session = AsyncMock()
    session.add = lambda obj: setattr(session, "_added", obj)

    await insert_schedule_revision(
        session,
        id="sr1",
        persona_id="chiwei",
        content="today...",
        reason="init",
        created_by="cron_morning",
    )
    added = session._added
    assert added.content == "today..."
    assert added.reason == "init"


@pytest.mark.asyncio
async def test_get_current_schedule_returns_latest():
    sr = AsyncMock()
    sr.id = "sr_latest"
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(sr))

    result = await get_current_schedule(session, persona_id="chiwei")
    assert result is sr
