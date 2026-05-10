"""Test upsert_note / list_note / resolve_note / delete_note tool implementations."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.tools.notes import (
    _delete_note_impl,
    _list_note_impl,
    _resolve_note_impl,
    _upsert_note_impl,
)


@asynccontextmanager
async def _fake_tx():
    yield


def _make_emit_tx_mock():
    captured: list = []

    async def _fake_emit_tx(ev):
        captured.append(ev)

    return _fake_emit_tx, captured


# ----- upsert_note: create -----

@pytest.mark.asyncio
async def test_upsert_note_create_emits_note_created():
    from app.domain.agent_tool_events import NoteCreated

    created = MagicMock(
        id="n_new", content="周五看电影", when_at=None,
        created_at=datetime(2026, 5, 10, tzinfo=UTC),
    )
    fake_emit, captured = _make_emit_tx_mock()
    with patch("app.agent.tools.notes.upsert_note_query",
               new=AsyncMock(return_value=created)) as up:
        with patch("app.agent.tools.notes.tx", _fake_tx):
            with patch("app.agent.tools.notes.emit_tx", fake_emit):
                out = await _upsert_note_impl(
                    persona_id="chiwei",
                    content="周五看电影",
                    when_at_raw=None,
                    note_id=None,
                )
    assert out["id"] == "n_new"
    up.assert_awaited_once()
    assert len(captured) == 1
    ev = captured[0]
    assert isinstance(ev, NoteCreated)
    assert ev.note_id == "n_new"


@pytest.mark.asyncio
async def test_upsert_note_rejects_empty_content():
    fake_emit, captured = _make_emit_tx_mock()
    with patch("app.agent.tools.notes.upsert_note_query",
               new=AsyncMock()) as up:
        with patch("app.agent.tools.notes.tx", _fake_tx):
            with patch("app.agent.tools.notes.emit_tx", fake_emit):
                out = await _upsert_note_impl(
                    persona_id="chiwei",
                    content="  ",
                    when_at_raw=None,
                    note_id=None,
                )
    assert "error" in out
    up.assert_not_awaited()
    assert captured == []


@pytest.mark.asyncio
async def test_upsert_note_parses_iso_when_at():
    when_iso = "2026-05-15T19:00:00+08:00"
    created = MagicMock(id="n_x", content="x", when_at=None,
                        created_at=datetime(2026, 5, 10, tzinfo=UTC))
    fake_emit, _ = _make_emit_tx_mock()
    with patch("app.agent.tools.notes.upsert_note_query",
               new=AsyncMock(return_value=created)) as up:
        with patch("app.agent.tools.notes.tx", _fake_tx):
            with patch("app.agent.tools.notes.emit_tx", fake_emit):
                await _upsert_note_impl(
                    persona_id="chiwei", content="x",
                    when_at_raw=when_iso, note_id=None,
                )
    kwargs = up.await_args.kwargs
    assert kwargs["when_at"] == datetime.fromisoformat(when_iso)


@pytest.mark.asyncio
async def test_upsert_note_rejects_bad_when_at():
    fake_emit, _ = _make_emit_tx_mock()
    with patch("app.agent.tools.notes.upsert_note_query",
               new=AsyncMock()) as up:
        with patch("app.agent.tools.notes.tx", _fake_tx):
            with patch("app.agent.tools.notes.emit_tx", fake_emit):
                out = await _upsert_note_impl(
                    persona_id="chiwei", content="x",
                    when_at_raw="not-a-date", note_id=None,
                )
    assert "error" in out
    up.assert_not_awaited()


# ----- upsert_note: update -----

@pytest.mark.asyncio
async def test_upsert_note_update_passes_note_id_and_does_not_emit():
    updated = MagicMock(
        id="n_abc", content="改后", when_at=None,
        created_at=datetime(2026, 5, 10, tzinfo=UTC),
    )
    fake_emit, captured = _make_emit_tx_mock()
    with patch("app.agent.tools.notes.upsert_note_query",
               new=AsyncMock(return_value=updated)) as up:
        with patch("app.agent.tools.notes.tx", _fake_tx):
            with patch("app.agent.tools.notes.emit_tx", fake_emit):
                out = await _upsert_note_impl(
                    persona_id="chiwei", content="改后",
                    when_at_raw=None, note_id="n_abc",
                )
    assert out["id"] == "n_abc"
    kwargs = up.await_args.kwargs
    assert kwargs["note_id"] == "n_abc"
    assert "when_at" in kwargs
    assert captured == []


@pytest.mark.asyncio
async def test_upsert_note_clear_when_at_translates_to_explicit_none():
    """when_at_raw='clear' → query receives when_at=None (not _UNSET)."""
    updated = MagicMock(id="n_abc", content="x", when_at=None,
                        created_at=datetime(2026, 5, 10, tzinfo=UTC))
    fake_emit, _ = _make_emit_tx_mock()
    with patch("app.agent.tools.notes.upsert_note_query",
               new=AsyncMock(return_value=updated)) as up:
        with patch("app.agent.tools.notes.tx", _fake_tx):
            with patch("app.agent.tools.notes.emit_tx", fake_emit):
                await _upsert_note_impl(
                    persona_id="chiwei", content="x",
                    when_at_raw="clear", note_id="n_abc",
                )
    assert up.await_args.kwargs["when_at"] is None


@pytest.mark.asyncio
async def test_upsert_note_unknown_id_returns_error():
    fake_emit, _ = _make_emit_tx_mock()
    with patch(
        "app.agent.tools.notes.upsert_note_query",
        new=AsyncMock(side_effect=LookupError("note not found: n_x")),
    ):
        with patch("app.agent.tools.notes.tx", _fake_tx):
            with patch("app.agent.tools.notes.emit_tx", fake_emit):
                out = await _upsert_note_impl(
                    persona_id="chiwei", content="x",
                    when_at_raw=None, note_id="n_x",
                )
    assert "error" in out
    assert "n_x" in out["error"]


# ----- list_note -----

@pytest.mark.asyncio
async def test_list_note_returns_items_with_when_label():
    rows = [
        MagicMock(
            id="n_1", content="周五看电影",
            when_at=datetime(2026, 5, 15, 19, 0, tzinfo=UTC),
            created_at=datetime(2026, 5, 9, tzinfo=UTC),
        ),
        MagicMock(
            id="n_2", content="想问妈妈那件事",
            when_at=None,
            created_at=datetime(2026, 5, 8, tzinfo=UTC),
        ),
    ]
    with patch("app.agent.tools.notes.list_active_notes_query",
               new=AsyncMock(return_value=rows)):
        out = await _list_note_impl(persona_id="chiwei")
    assert len(out["items"]) == 2
    assert out["items"][0]["note_id"] == "n_1"
    assert "when_label" in out["items"][0]
    assert out["items"][1]["when_at"] is None


@pytest.mark.asyncio
async def test_list_note_empty():
    with patch("app.agent.tools.notes.list_active_notes_query",
               new=AsyncMock(return_value=[])):
        out = await _list_note_impl(persona_id="chiwei")
    assert out == {"items": []}


# ----- delete_note -----

@pytest.mark.asyncio
async def test_delete_note_passes_reason():
    with patch("app.agent.tools.notes.delete_note_query",
               new=AsyncMock()) as dn:
        out = await _delete_note_impl(
            persona_id="chiwei", note_id="n_abc", reason="改主意了",
        )
    assert out == {"ok": True}
    dn.assert_awaited_once_with(note_id="n_abc", reason="改主意了")


@pytest.mark.asyncio
async def test_delete_note_rejects_empty_reason():
    with patch("app.agent.tools.notes.delete_note_query",
               new=AsyncMock()) as dn:
        out = await _delete_note_impl(
            persona_id="chiwei", note_id="n_abc", reason="  ",
        )
    assert "error" in out
    dn.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_note_rejects_empty_note_id():
    with patch("app.agent.tools.notes.delete_note_query",
               new=AsyncMock()) as dn:
        out = await _delete_note_impl(
            persona_id="chiwei", note_id="", reason="改主意了",
        )
    assert "error" in out
    dn.assert_not_awaited()


# ----- resolve_note (logic unchanged) -----

@pytest.mark.asyncio
async def test_resolve_note_calls_query():
    with patch("app.agent.tools.notes.resolve_note_query", new=AsyncMock()) as rn:
        out = await _resolve_note_impl(
            persona_id="chiwei", note_id="n_1", resolution="看完了",
        )
    assert out == {"ok": True}
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
