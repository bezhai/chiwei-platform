"""Test user_abstracts section."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.sections.user_abstracts import build_user_abstracts_section


@pytest.mark.asyncio
async def test_empty_when_no_trigger_user():
    text = await build_user_abstracts_section(persona_id="chiwei", trigger_user_id=None, trigger_username=None)
    assert text == ""


@pytest.mark.asyncio
async def test_renders_user_and_relation_subjects():
    a1 = MagicMock(subject="user:u1", content="他是程序员", clarity="clear")
    a2 = MagicMock(subject="和 u1 的关系", content="我们最近吵架了", clarity="clear")
    with patch("app.memory.sections.user_abstracts.get_abstracts_by_subjects", new=AsyncMock(return_value=[a1, a2])):
        text = await build_user_abstracts_section(
            persona_id="chiwei",
            trigger_user_id="u1",
            trigger_username="浩南",
        )
    assert "浩南" in text
    assert "程序员" in text
    assert "吵架" in text


@pytest.mark.asyncio
async def test_fallback_label_when_no_username():
    a1 = MagicMock(subject="user:u1", content="他喜欢打篮球", clarity="clear")
    with patch("app.memory.sections.user_abstracts.get_abstracts_by_subjects", new=AsyncMock(return_value=[a1])):
        text = await build_user_abstracts_section(
            persona_id="chiwei",
            trigger_user_id="u1",
            trigger_username=None,
        )
    assert "该用户" in text
    assert "篮球" in text
