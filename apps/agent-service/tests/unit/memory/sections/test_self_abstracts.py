"""Test self_abstracts section."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.sections.self_abstracts import build_self_abstracts_section


@pytest.mark.asyncio
async def test_returns_empty_when_no_abstracts():
    with patch("app.memory.sections.self_abstracts.get_abstracts_by_subjects", new=AsyncMock(return_value=[])):
        text = await build_self_abstracts_section(persona_id="chiwei")
    assert text == ""


@pytest.mark.asyncio
async def test_renders_bullet_list():
    a1 = MagicMock(content="我最近变温柔了", clarity="clear")
    a2 = MagicMock(content="我爱吃拉面", clarity="vague")
    with patch("app.memory.sections.self_abstracts.get_abstracts_by_subjects", new=AsyncMock(return_value=[a1, a2])):
        text = await build_self_abstracts_section(persona_id="chiwei")
    assert "温柔" in text
    assert "拉面" in text
    assert text.startswith("关于你自己")
