"""Test short-term fragment injection (§2.8)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.sections.short_term_fragments import build_short_term_fragments_section


@pytest.mark.asyncio
async def test_empty_when_no_fragments():
    with patch(
        "app.memory.sections.short_term_fragments.get_recent_fragments_for_injection",
        new=AsyncMock(return_value=[]),
    ):
        text = await build_short_term_fragments_section(
            persona_id="chiwei",
            chat_id="oc_a",
            trigger_user_id="u1",
        )
    assert text == ""


@pytest.mark.asyncio
async def test_renders_fragments_with_length_cap():
    f1 = MagicMock(
        id="f_1",
        content="刚才和浩南在 ka 群聊了新番，氛围不错",
        chat_id="oc_a",
        created_at=datetime(2026, 4, 18, 10, 0, tzinfo=UTC),
    )
    f2 = MagicMock(
        id="f_2",
        content="x" * 500,
        chat_id="oc_b",
        created_at=datetime(2026, 4, 18, 9, 30, tzinfo=UTC),
    )
    with patch(
        "app.memory.sections.short_term_fragments.get_recent_fragments_for_injection",
        new=AsyncMock(return_value=[f1, f2]),
    ):
        text = await build_short_term_fragments_section(
            persona_id="chiwei",
            chat_id="oc_a",
            trigger_user_id="u1",
        )
    assert "新番" in text
    assert len(text) < 1200
