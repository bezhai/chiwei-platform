"""Unit tests for context-injection query helpers (Plan C Task 1)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.data.models import AbstractMemory, Fragment
from app.data.queries import (
    count_abstracts_per_subject_prefix,
    get_abstracts_by_subjects,
    get_recent_abstract_titles,
    get_recent_fragments_for_injection,
)
from tests.unit.data._helpers import IterResult as _IterResult
from tests.unit.data._helpers import ScalarResult as _ScalarResult

MOD = "app.data.queries.memory_search"


@asynccontextmanager
async def _fake_auto_tx():
    yield


def _patch(session):
    patches = [
        patch(f"{MOD}.auto_tx", _fake_auto_tx),
        patch(f"{MOD}.current_session", return_value=session),
    ]
    for p in patches:
        p.start()
    return patches


def _stop(patches):
    for p in patches:
        p.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_abstract(
    id: str,
    subject: str,
    persona_id: str = "chiwei",
    clarity: str = "clear",
) -> AbstractMemory:
    a = AbstractMemory(
        id=id,
        persona_id=persona_id,
        subject=subject,
        content=f"content-{id}",
        created_by="test",
        clarity=clarity,
    )
    a.last_touched_at = datetime(2025, 1, 1, tzinfo=UTC)
    return a


def _make_fragment(
    id: str,
    chat_id: str | None,
    persona_id: str = "chiwei",
    clarity: str = "clear",
) -> Fragment:
    f = Fragment(
        id=id,
        persona_id=persona_id,
        content=f"frag-{id}",
        source="conv",
        chat_id=chat_id,
        clarity=clarity,
    )
    f.created_at = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
    return f


# ---------------------------------------------------------------------------
# get_abstracts_by_subjects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_abstracts_by_subjects_empty_subjects_returns_empty():
    session = AsyncMock()
    patches = _patch(session)
    try:
        result = await get_abstracts_by_subjects(persona_id="chiwei", subjects=[])
        assert result == []
        session.execute.assert_not_awaited()
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_get_abstracts_by_subjects_grouping_and_limit():
    """Each subject gets at most limit_per_subject entries, order matches subjects."""
    a1 = _make_abstract("a1", "user:alice")
    a2 = _make_abstract("a2", "user:alice")
    a3 = _make_abstract("a3", "user:bob")

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([a1, a2, a3]))

    patches = _patch(session)
    try:
        result = await get_abstracts_by_subjects(
            persona_id="chiwei",
            subjects=["user:alice", "user:bob"],
            limit_per_subject=1,
        )
        assert len(result) == 2
        assert result[0] is a1
        assert result[1] is a3
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_get_abstracts_by_subjects_no_limit_exceeded():
    """All entries returned when count < limit_per_subject."""
    a1 = _make_abstract("a1", "topic:books")
    a2 = _make_abstract("a2", "topic:books")

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([a1, a2]))

    patches = _patch(session)
    try:
        result = await get_abstracts_by_subjects(
            persona_id="chiwei",
            subjects=["topic:books"],
            limit_per_subject=5,
        )
        assert len(result) == 2
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_get_abstracts_by_subjects_missing_subject_returns_empty_slot():
    """Subjects with no DB rows contribute nothing (no KeyError)."""
    a1 = _make_abstract("a1", "user:alice")
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([a1]))

    patches = _patch(session)
    try:
        result = await get_abstracts_by_subjects(
            persona_id="chiwei",
            subjects=["user:alice", "user:nobody"],
            limit_per_subject=5,
        )
        assert len(result) == 1
        assert result[0] is a1
    finally:
        _stop(patches)


# ---------------------------------------------------------------------------
# get_recent_abstract_titles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recent_abstract_titles_returns_list():
    a1 = _make_abstract("a1", "self:mood")
    a2 = _make_abstract("a2", "user:alice")

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([a1, a2]))

    patches = _patch(session)
    try:
        result = await get_recent_abstract_titles(persona_id="chiwei", limit=10)
        assert isinstance(result, list)
        assert result == [a1, a2]
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_get_recent_abstract_titles_empty_db_returns_empty():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([]))

    patches = _patch(session)
    try:
        result = await get_recent_abstract_titles(persona_id="chiwei")
        assert result == []
    finally:
        _stop(patches)


# ---------------------------------------------------------------------------
# count_abstracts_per_subject_prefix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_abstracts_per_subject_prefix_returns_int():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(7))

    patches = _patch(session)
    try:
        count = await count_abstracts_per_subject_prefix(
            persona_id="chiwei", prefix="user:"
        )
        assert count == 7
        assert isinstance(count, int)
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_count_abstracts_per_subject_prefix_zero():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(0))

    patches = _patch(session)
    try:
        count = await count_abstracts_per_subject_prefix(
            persona_id="chiwei", prefix="nonexistent:"
        )
        assert count == 0
    finally:
        _stop(patches)


# ---------------------------------------------------------------------------
# get_recent_fragments_for_injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recent_fragments_same_and_other_chat_grouping():
    """Same-chat fragments limited by max_same_chat; other chats deduplicated."""
    same1 = _make_fragment("s1", chat_id="chat-A")
    same2 = _make_fragment("s2", chat_id="chat-A")  # should be excluded at limit=1
    other1 = _make_fragment("o1", chat_id="chat-B")
    other2 = _make_fragment("o2", chat_id="chat-C")
    other3 = _make_fragment("o3", chat_id="chat-C")  # same chat as o2, deduplicated

    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_IterResult([same1, same2, other1, other2, other3])
    )

    patches = _patch(session)
    try:
        result = await get_recent_fragments_for_injection(
            persona_id="chiwei",
            chat_id="chat-A",
            _trigger_user_id=None,
            max_same_chat=1,
            max_other_chat=2,
            hours=4,
        )
        assert len(result) == 3
        assert same1 in result
        assert same2 not in result
        assert other1 in result
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_get_recent_fragments_no_chat_id():
    """When chat_id is None, all fragments go to other_chats bucket."""
    f1 = _make_fragment("f1", chat_id="chat-X")
    f2 = _make_fragment("f2", chat_id="chat-Y")

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([f1, f2]))

    patches = _patch(session)
    try:
        result = await get_recent_fragments_for_injection(
            persona_id="chiwei",
            chat_id=None,
            _trigger_user_id=None,
            max_same_chat=1,
            max_other_chat=2,
            hours=4,
        )
        assert len(result) == 2
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_get_recent_fragments_deduplicates_other_chats():
    """Multiple fragments from the same other chat → only first (newest) kept."""
    f1 = _make_fragment("f1", chat_id="chat-Z")
    f2 = _make_fragment("f2", chat_id="chat-Z")

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([f1, f2]))

    patches = _patch(session)
    try:
        result = await get_recent_fragments_for_injection(
            persona_id="chiwei",
            chat_id="different-chat",
            _trigger_user_id=None,
            max_same_chat=1,
            max_other_chat=5,
            hours=4,
        )
        assert len(result) == 1
        assert result[0] is f1
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_get_recent_fragments_empty_db():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_IterResult([]))

    patches = _patch(session)
    try:
        result = await get_recent_fragments_for_injection(
            persona_id="chiwei",
            chat_id="chat-A",
            _trigger_user_id=None,
        )
        assert result == []
    finally:
        _stop(patches)
