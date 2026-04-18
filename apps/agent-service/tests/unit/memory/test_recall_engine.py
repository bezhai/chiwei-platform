"""Test recall_engine run_recall() pure function."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.recall_engine import RecallResult, run_recall


@pytest.mark.asyncio
async def test_run_recall_returns_abstracts_with_supporting_facts():
    with patch("app.memory.recall_engine.embed_dense", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("app.memory.recall_engine.qdrant") as q:
            q.client.query_points = AsyncMock(
                return_value=MagicMock(points=[MagicMock(id="a_1", score=0.9, payload={"subject": "user:u1", "clarity": "clear"})])
            )
            with patch("app.memory.recall_engine.get_abstract_by_id", new=AsyncMock(return_value=MagicMock(id="a_1", subject="user:u1", content="他是程序员", clarity="clear"))):
                with patch("app.memory.recall_engine.list_edges_to", new=AsyncMock(return_value=[MagicMock(from_id="f_1", from_type="fact", edge_type="supports")])):
                    with patch("app.memory.recall_engine.get_fragment_by_id", new=AsyncMock(return_value=MagicMock(id="f_1", content="他说他在写 Rust", clarity="clear"))):
                        with patch("app.memory.recall_engine.touch_abstract", new=AsyncMock()):
                            with patch("app.memory.recall_engine.touch_fragment", new=AsyncMock()):
                                result = await run_recall(
                                    persona_id="chiwei",
                                    queries=["浩南"],
                                    k_abs=5,
                                    k_facts_per_abs=3,
                                )
    assert isinstance(result, RecallResult)
    assert len(result.abstracts) == 1
    assert result.abstracts[0]["id"] == "a_1"
    assert len(result.abstracts[0]["supporting_facts"]) == 1
    assert result.abstracts[0]["supporting_facts"][0]["id"] == "f_1"


@pytest.mark.asyncio
async def test_run_recall_filters_forgotten():
    with patch("app.memory.recall_engine.embed_dense", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("app.memory.recall_engine.qdrant") as q:
            q.client.query_points = AsyncMock(return_value=MagicMock(points=[]))
            result = await run_recall(
                persona_id="chiwei", queries=["x"], k_abs=5, k_facts_per_abs=3,
            )
    assert result.abstracts == []


@pytest.mark.asyncio
async def test_run_recall_empty_query_list_returns_empty():
    result = await run_recall(persona_id="chiwei", queries=[], k_abs=5, k_facts_per_abs=3)
    assert result.abstracts == []
