"""Test write_note / resolve_note tools."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.tools.notes import _resolve_note_impl, _write_note_impl


@asynccontextmanager
async def _fake_tx():
    yield


def _make_emit_tx_mock():
    captured: list = []

    async def _fake_emit_tx(ev):
        captured.append(ev)

    return _fake_emit_tx, captured


def _fake_current_session():
    """Return a stub session whose .flush() is a no-op AsyncMock."""
    s = MagicMock()
    s.flush = AsyncMock()
    return s


@pytest.mark.asyncio
async def test_write_note_creates_and_returns_id_with_active_list():
    from app.domain.agent_tool_events import NoteCreated

    active = [MagicMock(id="n_existing", content="已有笔记", when_at=None)]
    fake_emit, captured = _make_emit_tx_mock()
    with patch("app.agent.tools.notes.insert_note", new=AsyncMock()) as ins:
        with patch("app.agent.tools.notes.get_active_notes", new=AsyncMock(return_value=active)):
            with patch("app.agent.tools.notes.tx", _fake_tx):
                with patch("app.agent.tools.notes.emit_tx", fake_emit):
                    with patch("app.agent.tools.notes.current_session", return_value=_fake_current_session()):
                        out = await _write_note_impl(
                            persona_id="chiwei", content="周五看电影", when_at=None,
                        )
    assert "id" in out
    assert out["id"].startswith("n_")
    assert len(out["active_notes"]) == 1
    ins.assert_awaited_once()
    assert len(captured) == 1
    emitted = captured[0]
    assert isinstance(emitted, NoteCreated)
    assert emitted.note_id == out["id"]
    assert emitted.persona_id == "chiwei"


@pytest.mark.asyncio
async def test_write_note_rejects_empty_content():
    fake_emit, captured = _make_emit_tx_mock()
    with patch("app.agent.tools.notes.insert_note", new=AsyncMock()) as ins:
        with patch("app.agent.tools.notes.tx", _fake_tx):
            with patch("app.agent.tools.notes.emit_tx", fake_emit):
                out = await _write_note_impl(persona_id="chiwei", content="  ", when_at=None)
    assert "error" in out
    ins.assert_not_awaited()
    assert len(captured) == 0


@pytest.mark.asyncio
async def test_resolve_note_calls_query():
    with patch("app.agent.tools.notes.resolve_note_query", new=AsyncMock()) as rn:
        out = await _resolve_note_impl(
            persona_id="chiwei", note_id="n_1", resolution="看完了",
        )
    assert out.get("ok") is True
    rn.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_note_rejects_whitespace_resolution():
    with patch("app.agent.tools.notes.resolve_note_query", new=AsyncMock()) as rn:
        out = await _resolve_note_impl(
            persona_id="chiwei", note_id="n_1", resolution="  ",
        )
    assert "error" in out
    rn.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_note_rejects_empty_note_id():
    with patch("app.agent.tools.notes.resolve_note_query", new=AsyncMock()) as rn:
        out = await _resolve_note_impl(
            persona_id="chiwei", note_id="", resolution="看完了",
        )
    assert "error" in out
    rn.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_note_passes_when_at_through():
    from datetime import UTC, datetime

    from app.domain.agent_tool_events import NoteCreated

    when = datetime(2026, 4, 18, 19, 0, tzinfo=UTC)
    fake_emit, captured = _make_emit_tx_mock()
    with patch("app.agent.tools.notes.insert_note", new=AsyncMock()) as ins:
        with patch("app.agent.tools.notes.get_active_notes", new=AsyncMock(return_value=[])):
            with patch("app.agent.tools.notes.tx", _fake_tx):
                with patch("app.agent.tools.notes.emit_tx", fake_emit):
                    with patch("app.agent.tools.notes.current_session", return_value=_fake_current_session()):
                        out = await _write_note_impl(
                            persona_id="chiwei", content="周五看电影", when_at=when,
                        )
    assert ins.await_args.kwargs["when_at"] == when
    assert len(captured) == 1
    emitted = captured[0]
    assert isinstance(emitted, NoteCreated)
    assert emitted.note_id == out["id"]
    assert emitted.persona_id == "chiwei"
