"""Test v4 basic CRUD queries — unit tests with mocked current_session()."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

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
from tests.unit.data._helpers import IterResult as _IterResult
from tests.unit.data._helpers import ScalarResult as _ScalarResult


@asynccontextmanager
async def _fake_auto_tx():
    yield


def _patch_module(mod: str, session):
    """Return list of started patches that route current_session()/auto_tx() to *session*."""
    patches = [
        patch(f"{mod}.auto_tx", _fake_auto_tx),
        patch(f"{mod}.current_session", return_value=session),
    ]
    for p in patches:
        p.start()
    return patches


def _stop(patches):
    for p in patches:
        p.stop()


# memory.py
@pytest.mark.asyncio
async def test_insert_fragment_adds_fragment_to_session():
    session = AsyncMock()
    session.add = lambda obj: setattr(session, "_added", obj)
    patches = _patch_module("app.data.queries.memory", session)
    try:
        await insert_fragment(
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
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_touch_fragment_executes_update():
    session = AsyncMock()
    session.execute = AsyncMock()
    patches = _patch_module("app.data.queries.memory", session)
    try:
        await touch_fragment("f1")
        session.execute.assert_awaited_once()
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_insert_abstract_memory_adds_to_session():
    session = AsyncMock()
    session.add = lambda obj: setattr(session, "_added", obj)
    patches = _patch_module("app.data.queries.memory", session)
    try:
        await insert_abstract_memory(
            id="a1",
            persona_id="chiwei",
            subject="user:u1",
            content="abstract",
            created_by="chiwei",
        )
        added = session._added
        assert isinstance(added, AbstractMemory)
        assert added.subject == "user:u1"
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_touch_abstract_executes_update():
    session = AsyncMock()
    session.execute = AsyncMock()
    patches = _patch_module("app.data.queries.memory", session)
    try:
        await touch_abstract("a1")
        session.execute.assert_awaited_once()
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_count_abstracts_by_persona_returns_int():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(5))
    patches = _patch_module("app.data.queries.memory", session)
    try:
        cnt = await count_abstracts_by_persona(persona_id="chiwei")
        assert cnt == 5
    finally:
        _stop(patches)


# memory_edges.py
@pytest.mark.asyncio
async def test_insert_memory_edge_adds_to_session():
    session = AsyncMock()
    session.add = lambda obj: setattr(session, "_added", obj)
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        await insert_memory_edge(
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
    finally:
        _stop(patches)


# memory_edges.py — notes
@pytest.mark.asyncio
async def test_insert_note_adds_to_session():
    session = AsyncMock()
    session.add = lambda obj: setattr(session, "_added", obj)
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        await insert_note(
            id="n1",
            persona_id="chiwei",
            content="周五看电影",
        )
        added = session._added
        assert isinstance(added, Note)
        assert added.content == "周五看电影"
        assert added.when_at is None
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_get_active_notes_returns_list():
    note = Note(id="n1", persona_id="chiwei", content="x")
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([note]))
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        result = await get_active_notes(persona_id="chiwei")
        assert result == [note]
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_resolve_note_executes_update():
    session = AsyncMock()
    session.execute = AsyncMock()
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        await resolve_note(note_id="n1", resolution="done")
        session.execute.assert_awaited_once()
    finally:
        _stop(patches)


# schedule.py
@pytest.mark.asyncio
async def test_insert_schedule_revision_adds_to_session():
    session = AsyncMock()
    session.add = lambda obj: setattr(session, "_added", obj)
    patches = _patch_module("app.data.queries.schedule", session)
    try:
        await insert_schedule_revision(
            id="sr1",
            persona_id="chiwei",
            content="today...",
            reason="init",
            created_by="cron_morning",
        )
        added = session._added
        assert added.content == "today..."
        assert added.reason == "init"
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_get_current_schedule_returns_latest():
    sr = AsyncMock()
    sr.id = "sr_latest"
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(sr))
    patches = _patch_module("app.data.queries.schedule", session)
    try:
        result = await get_current_schedule(persona_id="chiwei")
        assert result is sr
    finally:
        _stop(patches)
