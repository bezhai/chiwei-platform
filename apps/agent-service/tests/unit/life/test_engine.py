"""Tests for Life Engine tick (tool-based v4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.life.engine import tick
from app.life.tool import CommitResult

CST = timezone(timedelta(hours=8))
MODULE = "app.life.engine"


def _make_row(**kwargs):
    row = MagicMock()
    row.current_state = kwargs.get("current_state", "发呆")
    row.activity_type = kwargs.get("activity_type", "idle")
    row.response_mood = kwargs.get("response_mood", "无聊")
    row.skip_until = kwargs.get("skip_until", None)
    row.state_end_at = kwargs.get("state_end_at", None)
    row.created_at = kwargs.get("created_at", datetime.now(CST))
    return row


@pytest.mark.asyncio
async def test_tick_returns_none_when_skip_until_future():
    future = datetime.now(CST) + timedelta(minutes=10)
    row = _make_row(skip_until=future)
    with patch(f"{MODULE}.Q.find_latest_life_state", new=AsyncMock(return_value=row)):
        result = await tick("chiwei")
    assert result is None


@pytest.mark.asyncio
async def test_tick_returns_none_when_state_end_at_future():
    future = datetime.now(CST) + timedelta(minutes=30)
    row = _make_row(state_end_at=future)
    with (
        patch(f"{MODULE}.Q.find_latest_life_state", new=AsyncMock(return_value=row)),
        patch(f"{MODULE}._think", new=AsyncMock()) as think_mock,
    ):
        result = await tick("chiwei")
    assert result is None
    think_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_force_bypasses_skip_until():
    future = datetime.now(CST) + timedelta(minutes=10)
    row = _make_row(
        skip_until=future,
        state_end_at=datetime.now(CST) + timedelta(minutes=30),
    )
    expected = CommitResult(ok=True, is_refresh=False, life_state_id=1)
    with (
        patch(f"{MODULE}.Q.find_latest_life_state", new=AsyncMock(return_value=row)),
        patch(f"{MODULE}._think", new=AsyncMock(return_value=expected)),
    ):
        result = await tick("chiwei", force=True)
    assert result is expected


@pytest.mark.asyncio
async def test_tick_retries_once_when_llm_misses_tool():
    row = _make_row()
    success = CommitResult(ok=True, is_refresh=False, life_state_id=2)
    think_mock = AsyncMock(side_effect=[None, success])
    with (
        patch(f"{MODULE}.Q.find_latest_life_state", new=AsyncMock(return_value=row)),
        patch(f"{MODULE}._think", new=think_mock),
    ):
        result = await tick("chiwei")
    assert result is success
    assert think_mock.await_count == 2


@pytest.mark.asyncio
async def test_tick_gives_up_after_max_retries():
    row = _make_row()
    think_mock = AsyncMock(return_value=None)
    with (
        patch(f"{MODULE}.Q.find_latest_life_state", new=AsyncMock(return_value=row)),
        patch(f"{MODULE}._think", new=think_mock),
    ):
        result = await tick("chiwei")
    assert result is None
    assert think_mock.await_count == 2  # MAX_TICK_ATTEMPTS


@pytest.mark.asyncio
async def test_tick_returns_failed_validation_result():
    """When tool returns CommitResult(ok=False), tick returns it (no retry)."""
    row = _make_row()
    failed = CommitResult(ok=False, error="state_end_at 必须大于 now")
    with (
        patch(f"{MODULE}.Q.find_latest_life_state", new=AsyncMock(return_value=row)),
        patch(f"{MODULE}._think", new=AsyncMock(return_value=failed)),
    ):
        result = await tick("chiwei")
    assert result is failed


@pytest.mark.asyncio
async def test_tick_no_prev_state_passes_none():
    """When no prev row, tick calls _think with prev_state_row=None."""
    success = CommitResult(ok=True, life_state_id=3)
    think_mock = AsyncMock(return_value=success)
    with (
        patch(f"{MODULE}.Q.find_latest_life_state", new=AsyncMock(return_value=None)),
        patch(f"{MODULE}._think", new=think_mock),
    ):
        result = await tick("chiwei")
    assert result is success
    assert think_mock.await_args.args[0] is None  # prev_state_row arg
