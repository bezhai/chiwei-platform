"""Test v4 basic CRUD queries — unit tests with mocked current_session()."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.data.models import AbstractMemory, Fragment, MemoryEdge, Note
from app.data.queries import (
    count_abstracts_by_persona,
    get_current_schedule,
    insert_abstract_memory,
    insert_fragment,
    insert_memory_edge,
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
async def test_resolve_note_executes_update():
    session = AsyncMock()
    session.execute = AsyncMock()
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        await resolve_note(note_id="n1", resolution="done")
        session.execute.assert_awaited_once()
    finally:
        _stop(patches)


# memory_edges.py — upsert_note (Notes redesign 2026-05-10)
@pytest.mark.asyncio
async def test_upsert_note_create_when_no_id():
    session = AsyncMock()
    session.add = lambda obj: setattr(session, "_added", obj)
    session.flush = AsyncMock()
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import upsert_note
        added = await upsert_note(
            persona_id="chiwei",
            content="周五看电影",
        )
        assert isinstance(added, Note)
        assert added.id.startswith("n_")
        assert added.content == "周五看电影"
        assert added.when_at is None
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_upsert_note_update_content_only():
    existing = Note(id="n_abc", persona_id="chiwei", content="old", when_at=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(existing))
    session.flush = AsyncMock()
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import upsert_note
        out = await upsert_note(
            persona_id="chiwei",
            content="new content",
            note_id="n_abc",
        )
        assert out.content == "new content"
        assert out.when_at is None  # _UNSET = don't change; was None, stays None
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_upsert_note_update_when_at():
    from datetime import UTC
    from datetime import datetime as _dt
    existing = Note(id="n_abc", persona_id="chiwei", content="old", when_at=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(existing))
    session.flush = AsyncMock()
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import upsert_note
        new_when = _dt(2026, 5, 17, 12, 0, tzinfo=UTC)
        out = await upsert_note(
            persona_id="chiwei",
            content="old",
            when_at=new_when,
            note_id="n_abc",
        )
        assert out.when_at == new_when
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_upsert_note_clear_when_at_with_explicit_none():
    from datetime import UTC
    from datetime import datetime as _dt
    existing = Note(
        id="n_abc",
        persona_id="chiwei",
        content="old",
        when_at=_dt(2026, 5, 1, 12, 0, tzinfo=UTC),
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(existing))
    session.flush = AsyncMock()
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import upsert_note
        out = await upsert_note(
            persona_id="chiwei",
            content="old",
            when_at=None,  # explicit None = clear
            note_id="n_abc",
        )
        assert out.when_at is None
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_upsert_note_unknown_id_raises():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(None))
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import upsert_note
        with pytest.raises(LookupError, match="note not found"):
            await upsert_note(
                persona_id="chiwei",
                content="x",
                note_id="n_does_not_exist",
            )
    finally:
        _stop(patches)


# memory_edges.py — list_active_notes (Notes redesign 2026-05-10)
@pytest.mark.asyncio
async def test_list_active_notes_filters_resolved_and_deleted():
    n_active = Note(id="n_active", persona_id="chiwei", content="alive")
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([n_active]))
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import list_active_notes
        result = await list_active_notes(persona_id="chiwei")
        assert result == [n_active]
        # WHERE clause filters both resolved_at and deleted_at IS NULL.
        stmt = session.execute.await_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "resolved_at IS NULL" in compiled
        assert "deleted_at IS NULL" in compiled
    finally:
        _stop(patches)


# memory_edges.py — select_notes_for_context (Notes redesign 2026-05-10)
@pytest.mark.asyncio
async def test_select_notes_for_context_window_and_limit():
    """SQL has the 3-day / 7-day window + 15-row limit."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([]))
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import select_notes_for_context
        await select_notes_for_context(persona_id="chiwei")
        stmt = session.execute.await_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "LIMIT 15" in compiled
        assert "when_at" in compiled
        assert "created_at" in compiled
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_select_notes_for_context_returns_results():
    n = Note(id="n_1", persona_id="chiwei", content="x")
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([n]))
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import select_notes_for_context
        result = await select_notes_for_context(persona_id="chiwei")
        assert result == [n]
    finally:
        _stop(patches)


# memory_edges.py — delete_note (Notes redesign 2026-05-10)
@pytest.mark.asyncio
async def test_delete_note_soft_deletes():
    session = AsyncMock()
    session.execute = AsyncMock()
    patches = _patch_module("app.data.queries.memory_edges", session)
    try:
        from app.data.queries import delete_note
        await delete_note(note_id="n_abc", reason="改主意了")
        session.execute.assert_awaited_once()
        stmt = session.execute.await_args.args[0]
        compiled = str(stmt)
        assert "UPDATE notes" in compiled
        assert "deleted_at" in compiled
        assert "delete_reason" in compiled
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
