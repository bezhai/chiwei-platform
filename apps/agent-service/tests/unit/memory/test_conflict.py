"""Test conflict detection for abstract commits."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.conflict import detect_conflict


@pytest.mark.asyncio
async def test_detect_conflict_returns_hint_on_high_similarity():
    existing = MagicMock(id="a_old", content="他不爱吃甜食", clarity="clear")
    with patch("app.memory.conflict.get_abstracts_by_subject", new=AsyncMock(return_value=[existing])):
        with patch("app.memory.conflict.embed_dense", new=AsyncMock(side_effect=[[1.0] + [0.0]*1023, [0.99] + [0.01]*1023])):
            hint = await detect_conflict(
                persona_id="chiwei", subject="浩南",
                content="他今天主动买了奶茶",
                similarity_threshold=0.7,
            )
    assert hint is not None
    assert hint["conflicting_abstract_id"] == "a_old"


@pytest.mark.asyncio
async def test_detect_conflict_returns_none_when_low_similarity():
    existing = MagicMock(id="a_old", content="他是工程师", clarity="clear")
    with patch("app.memory.conflict.get_abstracts_by_subject", new=AsyncMock(return_value=[existing])):
        with patch("app.memory.conflict.embed_dense", new=AsyncMock(side_effect=[[1.0] + [0.0]*1023, [0.0]*1023 + [1.0]])):
            hint = await detect_conflict(
                persona_id="chiwei", subject="浩南",
                content="他喜欢跑步", similarity_threshold=0.7,
            )
    assert hint is None


@pytest.mark.asyncio
async def test_detect_conflict_empty_subject_returns_none():
    with patch("app.memory.conflict.get_abstracts_by_subject", new=AsyncMock(return_value=[])):
        hint = await detect_conflict(
            persona_id="chiwei", subject="new_subject",
            content="first fact", similarity_threshold=0.7,
        )
    assert hint is None
