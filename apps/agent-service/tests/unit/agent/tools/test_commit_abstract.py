"""Test commit_abstract_memory tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.tools.commit_abstract import _commit_abstract_impl


@pytest.mark.asyncio
async def test_commit_writes_abstract_and_edges():
    with patch("app.agent.tools.commit_abstract.detect_conflict", new=AsyncMock(return_value=None)):
        with patch("app.agent.tools.commit_abstract.get_fragment_by_id", new=AsyncMock(return_value=MagicMock())):
            with patch("app.agent.tools.commit_abstract.insert_abstract_memory", new=AsyncMock()) as ins_a:
                with patch("app.agent.tools.commit_abstract.insert_memory_edge", new=AsyncMock()) as ins_e:
                    with patch("app.agent.tools.commit_abstract.enqueue_abstract_vectorize", new=AsyncMock()) as enq:
                        out = await _commit_abstract_impl(
                            persona_id="chiwei",
                            subject="浩南",
                            content="他最近压力大",
                            supported_by_fact_ids=["f_1", "f_2"],
                            reasoning=None,
                        )
    assert "id" in out
    assert out["conflict_hint"] is None
    ins_a.assert_awaited_once()
    assert ins_e.await_count == 2
    enq.assert_awaited_once()


@pytest.mark.asyncio
async def test_commit_returns_conflict_hint():
    hint = {"conflicting_abstract_id": "a_old", "similarity": 0.91, "conflicting_content": "他不爱甜"}
    with patch("app.agent.tools.commit_abstract.detect_conflict", new=AsyncMock(return_value=hint)):
        with patch("app.agent.tools.commit_abstract.insert_abstract_memory", new=AsyncMock()) as ins_a:
            with patch("app.agent.tools.commit_abstract.insert_memory_edge", new=AsyncMock()) as ins_e:
                with patch("app.agent.tools.commit_abstract.enqueue_abstract_vectorize", new=AsyncMock()):
                    out = await _commit_abstract_impl(
                        persona_id="chiwei", subject="浩南",
                        content="他喝奶茶了", supported_by_fact_ids=None, reasoning=None,
                    )
    ins_a.assert_awaited_once()
    ins_e.assert_not_awaited()
    assert out["conflict_hint"] == hint


@pytest.mark.asyncio
async def test_commit_validates_fact_ids_exist():
    with patch("app.agent.tools.commit_abstract.detect_conflict", new=AsyncMock(return_value=None)):
        with patch("app.agent.tools.commit_abstract.get_fragment_by_id", new=AsyncMock(return_value=None)):
            with patch("app.agent.tools.commit_abstract.insert_abstract_memory", new=AsyncMock()) as ins_a:
                out = await _commit_abstract_impl(
                    persona_id="chiwei", subject="浩南",
                    content="x", supported_by_fact_ids=["f_missing"], reasoning=None,
                )
    assert "error" in out
    ins_a.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_rejects_whitespace_only_subject():
    with patch("app.agent.tools.commit_abstract.insert_abstract_memory", new=AsyncMock()) as ins_a:
        out = await _commit_abstract_impl(
            persona_id="chiwei",
            subject="   ",
            content="some content",
            supported_by_fact_ids=None,
            reasoning=None,
        )
    assert "error" in out
    ins_a.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_rejects_whitespace_only_content():
    with patch("app.agent.tools.commit_abstract.insert_abstract_memory", new=AsyncMock()) as ins_a:
        out = await _commit_abstract_impl(
            persona_id="chiwei",
            subject="浩南",
            content="   ",
            supported_by_fact_ids=None,
            reasoning=None,
        )
    assert "error" in out
    ins_a.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_passes_reasoning_to_edge():
    with patch("app.agent.tools.commit_abstract.detect_conflict", new=AsyncMock(return_value=None)):
        with patch("app.agent.tools.commit_abstract.get_fragment_by_id", new=AsyncMock(return_value=MagicMock())):
            with patch("app.agent.tools.commit_abstract.insert_abstract_memory", new=AsyncMock()):
                with patch("app.agent.tools.commit_abstract.insert_memory_edge", new=AsyncMock()) as ins_e:
                    with patch("app.agent.tools.commit_abstract.enqueue_abstract_vectorize", new=AsyncMock()):
                        await _commit_abstract_impl(
                            persona_id="chiwei",
                            subject="浩南",
                            content="他最近压力大",
                            supported_by_fact_ids=["f_1"],
                            reasoning="因为他连续三天加班到凌晨",
                        )
    assert ins_e.await_args.kwargs["reason"] == "因为他连续三天加班到凌晨"
