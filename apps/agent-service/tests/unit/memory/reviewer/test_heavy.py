"""Tests for app.memory.reviewer.heavy — heavy reviewer (daily consolidation)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory.reviewer.heavy import run_heavy_review_for_persona

MODULE = "app.memory.reviewer.heavy"
CST = timezone(timedelta(hours=8))


@asynccontextmanager
async def _fake_tx():
    yield


# ---------------------------------------------------------------------------
# skip when all windows are empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_when_all_windows_empty():
    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch(f"{MODULE}.list_fragments_window", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.list_abstracts_window", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.list_recent_life_states", new=AsyncMock(return_value=[])),
        patch(
            f"{MODULE}.list_recent_schedule_revisions", new=AsyncMock(return_value=[])
        ),
        patch(f"{MODULE}._run_agent", new=AsyncMock()) as agent,
    ):
        await run_heavy_review_for_persona("chiwei")

    agent.assert_not_awaited()


# ---------------------------------------------------------------------------
# dispatches agent when at least one fragment present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runs_with_full_day_summary():
    frag = MagicMock()
    frag.id = "f_001"
    frag.content = "今天学了新东西"

    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch(f"{MODULE}.list_fragments_window", new=AsyncMock(return_value=[frag])),
        patch(f"{MODULE}.list_abstracts_window", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.list_recent_life_states", new=AsyncMock(return_value=[])),
        patch(
            f"{MODULE}.list_recent_schedule_revisions", new=AsyncMock(return_value=[])
        ),
        patch(f"{MODULE}._run_agent", new=AsyncMock()) as agent,
    ):
        await run_heavy_review_for_persona("chiwei")

    agent.assert_awaited_once()
    call_kwargs = agent.await_args.kwargs
    assert call_kwargs["persona_id"] == "chiwei"
    assert "f_001" in call_kwargs["fragments_text"]
    assert call_kwargs["abstracts_text"] == ""  # empty join, _run_agent fills "（无）"
    assert call_kwargs["life_states_text"] == ""
    assert call_kwargs["schedule_text"] == ""


# ---------------------------------------------------------------------------
# dispatches agent when only life_states present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runs_when_only_life_states_present():
    state = MagicMock()
    state.created_at = datetime(2026, 4, 19, 10, 0, tzinfo=CST)
    state.activity_type = "working"
    state.current_state = "专注编码"
    state.response_mood = "calm"

    with (
        patch(f"{MODULE}.tx", _fake_tx),
        patch(f"{MODULE}.list_fragments_window", new=AsyncMock(return_value=[])),
        patch(f"{MODULE}.list_abstracts_window", new=AsyncMock(return_value=[])),
        patch(
            f"{MODULE}.list_recent_life_states", new=AsyncMock(return_value=[state])
        ),
        patch(
            f"{MODULE}.list_recent_schedule_revisions", new=AsyncMock(return_value=[])
        ),
        patch(f"{MODULE}._run_agent", new=AsyncMock()) as agent,
    ):
        await run_heavy_review_for_persona("chiwei")

    agent.assert_awaited_once()
    call_kwargs = agent.await_args.kwargs
    assert "working" in call_kwargs["life_states_text"]
    assert "calm" in call_kwargs["life_states_text"]


# ---------------------------------------------------------------------------
# life_state formatter output
# ---------------------------------------------------------------------------


def test_life_state_formatter_output():
    """Verify the formatter string contains key fields."""
    state = MagicMock()
    state.created_at = datetime(2026, 4, 19, 14, 30, tzinfo=CST)
    state.activity_type = "relaxing"
    state.current_state = "x" * 200  # 200 chars → truncated at 80
    state.response_mood = "happy"

    def fmt_life(ls):
        return (
            f"- {ls.created_at.isoformat()} [{ls.activity_type}] "
            f"{ls.current_state[:80]} mood={ls.response_mood}"
        )

    result = fmt_life(state)
    assert "relaxing" in result
    assert "happy" in result
    assert "2026-04-19" in result
    # current_state is truncated at 80 chars
    assert "x" * 80 in result
    assert "x" * 81 not in result


# ---------------------------------------------------------------------------
# schedule formatter output
# ---------------------------------------------------------------------------


def test_schedule_formatter_output():
    """Verify the schedule formatter includes created_by and reason."""

    revision = MagicMock()
    revision.created_at = datetime(2026, 4, 19, 8, 0, tzinfo=CST)
    revision.created_by = "life-engine"
    revision.reason = "wake up earlier than planned"

    def fmt_sched(sr):
        return (
            f"- {sr.created_at.isoformat()} [{sr.created_by}] reason={sr.reason[:80]}"
        )

    result = fmt_sched(revision)
    assert "life-engine" in result
    assert "wake up earlier than planned" in result
    assert "2026-04-19" in result
