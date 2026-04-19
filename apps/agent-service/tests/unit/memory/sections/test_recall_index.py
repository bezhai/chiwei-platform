"""Test recall_index section."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.sections.recall_index import build_recall_index_section


@pytest.mark.asyncio
async def test_empty_when_no_memory():
    with patch("app.memory.sections.recall_index.count_abstracts_by_persona", new=AsyncMock(return_value=0)):
        with patch("app.memory.sections.recall_index.get_recent_abstract_titles", new=AsyncMock(return_value=[])):
            text = await build_recall_index_section(persona_id="chiwei")
    assert text == ""


@pytest.mark.asyncio
async def test_renders_counts_and_recent_titles():
    titles = [MagicMock(subject="浩南", content="他最近压力大"), MagicMock(subject="学习", content="我开始学 Rust")]
    with patch("app.memory.sections.recall_index.count_abstracts_by_persona", new=AsyncMock(return_value=50)):
        with patch("app.memory.sections.recall_index.get_recent_abstract_titles", new=AsyncMock(return_value=titles)):
            text = await build_recall_index_section(persona_id="chiwei")
    assert "50" in text
    assert "浩南" in text
    assert "学习" in text
    assert "recall" in text.lower()
