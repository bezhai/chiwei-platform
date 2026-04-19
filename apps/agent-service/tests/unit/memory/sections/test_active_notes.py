"""Test active_notes section."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.sections.active_notes import build_active_notes_section


@pytest.mark.asyncio
async def test_empty_when_no_notes():
    with patch("app.memory.sections.active_notes.get_active_notes", new=AsyncMock(return_value=[])):
        text = await build_active_notes_section(persona_id="chiwei")
    assert text == ""


@pytest.mark.asyncio
async def test_renders_with_and_without_when_at():
    n1 = MagicMock(id="n_1", content="周五看电影", when_at=datetime(2026, 4, 24, 19, 0, tzinfo=UTC))
    n2 = MagicMock(id="n_2", content="想一下要不要学Rust", when_at=None)
    with patch("app.memory.sections.active_notes.get_active_notes", new=AsyncMock(return_value=[n1, n2])):
        text = await build_active_notes_section(persona_id="chiwei")
    assert "周五看电影" in text
    assert "Rust" in text
    assert "n_1" in text
