"""Tests for app.memory.reviewer.light — light reviewer (P0 window scan)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.reviewer.light import (
    _fmt_abstract,
    _fmt_fragment,
    _fmt_note,
    run_light_review,
)

MODULE = "app.memory.reviewer.light"
CST = timezone(timedelta(hours=8))


def _noop_session():
    """Async context manager that yields a dummy session."""

    @asynccontextmanager
    async def _cm():
        yield MagicMock()

    return _cm()


# ---------------------------------------------------------------------------
# noop when window is empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noop_when_empty_window():
    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.list_fragments_window", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.list_abstracts_window", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.get_active_notes", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}._run_reviewer_agent", new=AsyncMock()) as agent,
    ):
        result = await run_light_review(persona_id="chiwei", window_minutes=30)

    agent.assert_not_awaited()
    assert result is None


# ---------------------------------------------------------------------------
# dispatches agent when at least one fragment present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runs_agent_with_window_summary():
    frag = MagicMock()
    frag.id = "f_001"
    frag.content = "test fragment content"

    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.list_fragments_window", new=AsyncMock(return_value=[frag])),
        patch(f"{MODULE}.list_abstracts_window", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.get_active_notes", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}._run_reviewer_agent", new=AsyncMock()) as agent,
    ):
        await run_light_review(persona_id="chiwei", window_minutes=30)

    agent.assert_awaited_once()
    call_kwargs = agent.await_args.kwargs
    assert call_kwargs["persona_id"] == "chiwei"
    assert "f_001" in call_kwargs["fragments_text"]
    assert call_kwargs["abstracts_text"] == "（无）"
    assert call_kwargs["notes_text"] == "（无）"


# ---------------------------------------------------------------------------
# dispatches agent when only notes present (no fragments or abstracts)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runs_when_only_notes_present():
    note = MagicMock()
    note.id = "n_001"
    note.content = "some active note"
    note.when_at = None

    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.list_fragments_window", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.list_abstracts_window", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.get_active_notes", new=AsyncMock(return_value=[note])),
        patch(f"{MODULE}._run_reviewer_agent", new=AsyncMock()) as agent,
    ):
        await run_light_review(persona_id="chiwei", window_minutes=60)

    # Per spec: skip only when ALL three are empty; only-notes must dispatch
    agent.assert_awaited_once()
    call_kwargs = agent.await_args.kwargs
    assert call_kwargs["persona_id"] == "chiwei"
    assert "n_001" in call_kwargs["notes_text"]
    assert call_kwargs["fragments_text"] == "（无）"
    assert call_kwargs["abstracts_text"] == "（无）"


# ---------------------------------------------------------------------------
# formatters
# ---------------------------------------------------------------------------


def test_fmt_fragment_includes_id_and_truncates():
    frag = MagicMock()
    frag.id = "f_1"
    frag.content = "x" * 300  # 300 chars, should truncate at 200

    result = _fmt_fragment(frag)

    assert result.startswith("- [f_1]")
    assert len(result) < 300 + 20  # id prefix + truncated content
    # Content truncated at 200 chars
    assert "x" * 200 in result
    assert "x" * 201 not in result


def test_fmt_abstract_includes_subject():
    ab = MagicMock()
    ab.id = "a_2"
    ab.subject = "reading habits"
    ab.content = "likes sci-fi"

    result = _fmt_abstract(ab)

    assert "a_2" in result
    assert "subject=reading habits" in result
    assert "likes sci-fi" in result


def test_fmt_note_with_when_at():
    note = MagicMock()
    note.id = "n_3"
    note.content = "buy groceries"
    note.when_at = datetime(2026, 4, 19, 10, 0, tzinfo=CST)

    result = _fmt_note(note)

    assert "n_3" in result
    assert "buy groceries" in result
    assert "2026-04-19" in result


def test_fmt_note_without_when_at():
    note = MagicMock()
    note.id = "n_4"
    note.content = "vague todo"
    note.when_at = None

    result = _fmt_note(note)

    assert "n_4" in result
    assert "when=-" in result
