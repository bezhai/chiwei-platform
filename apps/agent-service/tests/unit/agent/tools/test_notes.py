"""Test write_note / resolve_note tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.tools.notes import _resolve_note_impl, _write_note_impl


@pytest.mark.asyncio
async def test_write_note_creates_and_returns_id_with_active_list():
    active = [MagicMock(id="n_existing", content="已有笔记", when_at=None)]
    with patch("app.agent.tools.notes.insert_note", new=AsyncMock()) as ins:
        with patch("app.agent.tools.notes.get_active_notes", new=AsyncMock(return_value=active)):
            out = await _write_note_impl(
                persona_id="chiwei", content="周五看电影", when_at=None,
            )
    assert "id" in out
    assert out["id"].startswith("n_")
    assert len(out["active_notes"]) == 1
    ins.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_note_rejects_empty_content():
    with patch("app.agent.tools.notes.insert_note", new=AsyncMock()) as ins:
        out = await _write_note_impl(persona_id="chiwei", content="  ", when_at=None)
    assert "error" in out
    ins.assert_not_awaited()


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

    when = datetime(2026, 4, 18, 19, 0, tzinfo=UTC)
    with patch("app.agent.tools.notes.insert_note", new=AsyncMock()) as ins:
        with patch("app.agent.tools.notes.get_active_notes", new=AsyncMock(return_value=[])):
            await _write_note_impl(
                persona_id="chiwei", content="周五看电影", when_at=when,
            )
    assert ins.await_args.kwargs["when_at"] == when
