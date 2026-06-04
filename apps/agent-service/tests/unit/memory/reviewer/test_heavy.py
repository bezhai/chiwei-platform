"""Tests for app.memory.reviewer.heavy — heavy reviewer (daily consolidation).

Heavy reviewer reads her latest subjective snapshot from the new lane-keyed
``LifeState`` (Task 3) instead of a day of ``life_engine_state`` history rows.
Schedules are no longer generated, so the heavy reviewer no longer reads any
schedule revisions.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.reviewer.heavy import run_heavy_review_for_persona

MODULE = "app.memory.reviewer.heavy"


@asynccontextmanager
async def _fake_tx():
    yield


# ---------------------------------------------------------------------------
# skip when day is empty (no fragments, no abstracts, no snapshot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_when_day_empty():
    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch(f"{MODULE}.list_fragments_window", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.list_abstracts_window", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.find_life_state", new=AsyncMock(return_value=None)),
        patch(f"{MODULE}.current_deployment_lane", return_value=None),
        patch(f"{MODULE}._run_agent", new=AsyncMock()) as agent,
    ):
        await run_heavy_review_for_persona("chiwei")

    agent.assert_not_awaited()


# ---------------------------------------------------------------------------
# dispatches agent when at least one fragment present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runs_with_fragments():
    frag = MagicMock()
    frag.id = "f_001"
    frag.content = "今天学了新东西"

    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch(f"{MODULE}.list_fragments_window", new=AsyncMock(return_value=[frag])),
        patch(f"{MODULE}.list_abstracts_window", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.find_life_state", new=AsyncMock(return_value=None)),
        patch(f"{MODULE}.current_deployment_lane", return_value=None),
        patch(f"{MODULE}._run_agent", new=AsyncMock()) as agent,
    ):
        await run_heavy_review_for_persona("chiwei")

    agent.assert_awaited_once()
    call_kwargs = agent.await_args.kwargs
    assert call_kwargs["persona_id"] == "chiwei"
    assert "f_001" in call_kwargs["fragments_text"]
    assert call_kwargs["life_state_text"] == ""  # empty, _run_agent fills "（无）"


# ---------------------------------------------------------------------------
# dispatches agent when only the latest snapshot present + reads it by lane
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runs_with_latest_snapshot_by_lane():
    snap = SimpleNamespace(
        current_state="专注编码",
        response_mood="calm",
        activity_type="working",
        observed_at="2026-04-19T10:00:00+00:00",
    )
    find = AsyncMock(return_value=snap)

    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch(f"{MODULE}.list_fragments_window", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.list_abstracts_window", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.find_life_state", new=find),
        patch(f"{MODULE}.current_deployment_lane", return_value="ppe-y"),
        patch(f"{MODULE}._run_agent", new=AsyncMock()) as agent,
    ):
        await run_heavy_review_for_persona("chiwei")

    # lane口径 == 写入端
    assert find.await_args.kwargs == {"lane": "ppe-y", "persona_id": "chiwei"}
    agent.assert_awaited_once()
    text = agent.await_args.kwargs["life_state_text"]
    assert "working" in text
    assert "calm" in text
    assert "专注编码" in text
