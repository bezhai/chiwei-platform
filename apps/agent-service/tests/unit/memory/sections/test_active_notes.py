"""Test active_notes section after windowed-injection redesign."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.data.models import Note
from app.memory.sections.active_notes import build_active_notes_section


def _note(*, id: str, content: str, when_at=None, created_at=None) -> Note:
    n = Note(
        id=id,
        persona_id="chiwei",
        content=content,
        when_at=when_at,
    )
    if created_at is not None:
        n.created_at = created_at
    return n


_NOW = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)


def _patch_now(monkeypatch):
    """Pin datetime.now in active_notes module to _NOW."""
    import app.memory.sections.active_notes as mod

    class _FixedDt:
        @staticmethod
        def now(tz=None):
            return _NOW.astimezone(tz) if tz else _NOW

    monkeypatch.setattr(mod, "datetime", _FixedDt)


@pytest.mark.asyncio
async def test_section_empty_when_no_active(monkeypatch):
    _patch_now(monkeypatch)
    with patch(
        "app.memory.sections.active_notes.select_notes_for_context",
        new=AsyncMock(return_value=[]),
    ):
        with patch(
            "app.memory.sections.active_notes.list_active_notes",
            new=AsyncMock(return_value=[]),
        ):
            text = await build_active_notes_section(persona_id="chiwei")
    assert text == ""


@pytest.mark.asyncio
async def test_section_renders_active_with_when_label(monkeypatch):
    _patch_now(monkeypatch)
    n1 = _note(
        id="n_1",
        content="周五和浩南看电影",
        when_at=_NOW + timedelta(days=2),
        created_at=_NOW - timedelta(hours=1),
    )
    n2 = _note(
        id="n_2",
        content="想问妈妈那件事",
        when_at=None,
        created_at=_NOW - timedelta(days=1),
    )
    with patch(
        "app.memory.sections.active_notes.select_notes_for_context",
        new=AsyncMock(return_value=[n1, n2]),
    ):
        with patch(
            "app.memory.sections.active_notes.list_active_notes",
            new=AsyncMock(return_value=[n1, n2]),
        ):
            text = await build_active_notes_section(persona_id="chiwei")
    assert "周五和浩南看电影 [还有 2 天] (id: n_1)" in text
    assert "想问妈妈那件事 [1 天前记的，没说时间] (id: n_2)" in text
    assert text.startswith("你的清单")


@pytest.mark.asyncio
async def test_section_appends_remainder_when_truncated(monkeypatch):
    """active_total > injected_count → append truncation hint."""
    _patch_now(monkeypatch)
    injected = [
        _note(id=f"n_{i}", content=f"事 {i}", when_at=None,
              created_at=_NOW - timedelta(hours=i))
        for i in range(15)
    ]
    all_active = injected + [
        _note(id="n_old", content="老事", when_at=None,
              created_at=_NOW - timedelta(days=30)),
        _note(id="n_old2", content="更老的", when_at=None,
              created_at=_NOW - timedelta(days=40)),
    ]
    with patch(
        "app.memory.sections.active_notes.select_notes_for_context",
        new=AsyncMock(return_value=injected),
    ):
        with patch(
            "app.memory.sections.active_notes.list_active_notes",
            new=AsyncMock(return_value=all_active),
        ):
            text = await build_active_notes_section(persona_id="chiwei")
    assert "（清单里还有 2 条更老的没列出来，用 list_note 看全部。）" in text


@pytest.mark.asyncio
async def test_section_only_remainder_hint_when_all_old(monkeypatch):
    """Active notes exist but none meet injection window → show only remainder hint."""
    _patch_now(monkeypatch)
    old = [
        _note(id="n_old", content="老事", when_at=None,
              created_at=_NOW - timedelta(days=30)),
        _note(id="n_old2", content="更老", when_at=None,
              created_at=_NOW - timedelta(days=40)),
        _note(id="n_old3", content="最老",
              when_at=_NOW - timedelta(days=10),
              created_at=_NOW - timedelta(days=12)),
    ]
    with patch(
        "app.memory.sections.active_notes.select_notes_for_context",
        new=AsyncMock(return_value=[]),
    ):
        with patch(
            "app.memory.sections.active_notes.list_active_notes",
            new=AsyncMock(return_value=old),
        ):
            text = await build_active_notes_section(persona_id="chiwei")
    assert text == "你的清单里还有 3 条没动的事（用 list_note 看）。"


@pytest.mark.asyncio
async def test_section_swallows_query_errors(monkeypatch):
    """Section must not crash if query layer fails."""
    _patch_now(monkeypatch)
    with patch(
        "app.memory.sections.active_notes.select_notes_for_context",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        text = await build_active_notes_section(persona_id="chiwei")
    assert text == ""
