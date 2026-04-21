"""Test state_only_refresh."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.life.state_sync import state_only_refresh
from app.life.tool import CommitResult

CST = timezone(timedelta(hours=8))
MODULE = "app.life.state_sync"


@pytest.mark.asyncio
async def test_noop_when_no_prev_state():
    with patch(f"{MODULE}.find_latest_life_state", new=AsyncMock(return_value=None)):
        result = await state_only_refresh(
            persona_id="chiwei", new_schedule_content="today..."
        )
    assert result is None


@pytest.mark.asyncio
async def test_in_segment_refresh_returns_commit_result():
    now = datetime.now(CST)
    prev = MagicMock(
        activity_type="study",
        current_state="doing homework",
        state_end_at=now + timedelta(minutes=30),
        response_mood="calm",
    )
    success = CommitResult(ok=True, is_refresh=True, life_state_id=10)
    with (
        patch(f"{MODULE}.find_latest_life_state", new=AsyncMock(return_value=prev)),
        patch(f"{MODULE}._run_refresh_agent", new=AsyncMock(return_value=success)),
    ):
        result = await state_only_refresh(
            persona_id="chiwei", new_schedule_content="冰淇淋到了"
        )
    assert result is success
    assert result.is_refresh is True


@pytest.mark.asyncio
async def test_agent_no_tool_call_returns_none():
    now = datetime.now(CST)
    prev = MagicMock(
        activity_type="study", current_state="...",
        state_end_at=now + timedelta(minutes=30), response_mood="calm",
    )
    with (
        patch(f"{MODULE}.find_latest_life_state", new=AsyncMock(return_value=prev)),
        patch(f"{MODULE}._run_refresh_agent", new=AsyncMock(return_value=None)),
    ):
        result = await state_only_refresh(
            persona_id="chiwei", new_schedule_content="x"
        )
    assert result is None


@pytest.mark.asyncio
async def test_uses_provided_now():
    """When now= is passed explicitly, it shouldn't recompute."""
    fixed_now = datetime(2026, 4, 19, 14, 30, tzinfo=CST)
    prev = MagicMock(
        activity_type="study", current_state="...",
        state_end_at=fixed_now + timedelta(minutes=30), response_mood="calm",
    )
    agent_mock = AsyncMock(return_value=CommitResult(ok=True))
    with (
        patch(f"{MODULE}.find_latest_life_state", new=AsyncMock(return_value=prev)),
        patch(f"{MODULE}._run_refresh_agent", new=agent_mock),
    ):
        await state_only_refresh(
            persona_id="chiwei", new_schedule_content="x", now=fixed_now,
        )
    assert agent_mock.await_args.kwargs["now"] == fixed_now
