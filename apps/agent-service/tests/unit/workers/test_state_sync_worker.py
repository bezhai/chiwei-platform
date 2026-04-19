"""Test sync_life_state_after_schedule arq job."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.life.tool import CommitResult
from app.workers.state_sync_worker import sync_life_state_after_schedule

MODULE = "app.workers.state_sync_worker"


def _noop_session():
    """Async context manager that yields a dummy session."""

    @asynccontextmanager
    async def _cm():
        yield MagicMock()

    return _cm()


@pytest.mark.asyncio
async def test_reads_revision_and_calls_refresh():
    rev = MagicMock(persona_id="chiwei", content="new plan")
    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.get_schedule_revision_by_id", new=AsyncMock(return_value=rev)),
        patch(f"{MODULE}.state_only_refresh", new=AsyncMock(return_value=None)) as sref,
    ):
        await sync_life_state_after_schedule(ctx={}, revision_id="sr_1")
    sref.assert_awaited_once()
    assert sref.await_args.kwargs["persona_id"] == "chiwei"
    assert sref.await_args.kwargs["new_schedule_content"] == "new plan"


@pytest.mark.asyncio
async def test_missing_revision_no_op():
    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.get_schedule_revision_by_id", new=AsyncMock(return_value=None)),
        patch(f"{MODULE}.state_only_refresh", new=AsyncMock()) as sref,
    ):
        await sync_life_state_after_schedule(ctx={}, revision_id="sr_missing")
    sref.assert_not_awaited()


@pytest.mark.asyncio
async def test_logs_success_result():
    """When state_only_refresh returns ok=True, log commit info. No raise."""
    rev = MagicMock(persona_id="chiwei", content="x")
    ok = CommitResult(ok=True, is_refresh=True, life_state_id=99)
    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.get_schedule_revision_by_id", new=AsyncMock(return_value=rev)),
        patch(f"{MODULE}.state_only_refresh", new=AsyncMock(return_value=ok)),
    ):
        await sync_life_state_after_schedule(ctx={}, revision_id="sr_2")  # no exception


@pytest.mark.asyncio
async def test_logs_failure_result():
    rev = MagicMock(persona_id="chiwei", content="x")
    failed = CommitResult(ok=False, error="state_end_at 必须大于 now")
    with (
        patch(f"{MODULE}.get_session", return_value=_noop_session()),
        patch(f"{MODULE}.get_schedule_revision_by_id", new=AsyncMock(return_value=rev)),
        patch(f"{MODULE}.state_only_refresh", new=AsyncMock(return_value=failed)),
    ):
        await sync_life_state_after_schedule(ctx={}, revision_id="sr_3")  # no exception
