"""Test recall tool — v4 Qdrant + graph semantic recall."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.agent.tools.recall import _recall_impl
from app.memory.recall_engine import RecallResult


@pytest.mark.asyncio
async def test_recall_returns_structured_json():
    rr = RecallResult(
        abstracts=[
            {
                "id": "a_1",
                "subject": "浩南",
                "content": "他最近压力大",
                "clarity": "clear",
                "supporting_facts": [
                    {"id": "f_1", "content": "他加班到凌晨", "clarity": "clear"}
                ],
            }
        ],
        facts=[],
    )
    with patch("app.agent.tools.recall.run_recall", new=AsyncMock(return_value=rr)):
        out = await _recall_impl(
            persona_id="chiwei",
            queries=["浩南最近怎么了"],
            k_abs=5,
            k_facts_per_abs=3,
        )
    assert out["abstracts"][0]["id"] == "a_1"
    assert out["abstracts"][0]["supporting_facts"][0]["id"] == "f_1"


@pytest.mark.asyncio
async def test_recall_accepts_batch_queries():
    with patch(
        "app.agent.tools.recall.run_recall",
        new=AsyncMock(return_value=RecallResult()),
    ) as rr:
        await _recall_impl(
            persona_id="chiwei",
            queries=["浩南", "学习"],
            k_abs=5,
            k_facts_per_abs=3,
        )
    call = rr.await_args
    assert call.kwargs["queries"] == ["浩南", "学习"]


@pytest.mark.asyncio
async def test_recall_empty_returns_structured_empty():
    with patch(
        "app.agent.tools.recall.run_recall",
        new=AsyncMock(return_value=RecallResult()),
    ):
        out = await _recall_impl(
            persona_id="chiwei", queries=["x"],
            k_abs=5, k_facts_per_abs=3,
        )
    assert out == {"abstracts": [], "facts": []}
