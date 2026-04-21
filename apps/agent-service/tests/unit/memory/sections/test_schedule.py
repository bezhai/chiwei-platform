"""Test schedule section."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.sections.schedule import build_schedule_section


@pytest.mark.asyncio
async def test_empty_when_no_schedule():
    with patch("app.memory.sections.schedule.get_current_schedule", new=AsyncMock(return_value=None)):
        text = await build_schedule_section(persona_id="chiwei")
    assert text == ""


@pytest.mark.asyncio
async def test_renders_schedule_content():
    sr = MagicMock(content="今天周五，早上 8-12 两节课...", reason="first draft")
    with patch("app.memory.sections.schedule.get_current_schedule", new=AsyncMock(return_value=sr)):
        text = await build_schedule_section(persona_id="chiwei")
    assert "今天" in text
