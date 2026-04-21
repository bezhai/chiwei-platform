"""Test update_schedule tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.agent.tools.update_schedule import _update_schedule_impl


@pytest.mark.asyncio
async def test_update_schedule_writes_revision_and_enqueues_sync():
    with patch("app.agent.tools.update_schedule.insert_schedule_revision", new=AsyncMock()) as ins:
        with patch("app.agent.tools.update_schedule.enqueue_state_sync", new=AsyncMock()) as enq:
            out = await _update_schedule_impl(
                persona_id="chiwei", content="今天...", reason="first draft",
                created_by="chiwei",
            )
    assert "revision_id" in out
    ins.assert_awaited_once()
    enq.assert_awaited_once_with(revision_id=out["revision_id"])


@pytest.mark.asyncio
async def test_update_schedule_rejects_empty():
    with patch("app.agent.tools.update_schedule.insert_schedule_revision", new=AsyncMock()) as ins:
        out = await _update_schedule_impl(
            persona_id="chiwei", content=" ", reason="", created_by="chiwei",
        )
    assert "error" in out
    ins.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_schedule_returns_success_even_if_enqueue_fails():
    """enqueue failure must not lose the written revision."""
    with patch("app.agent.tools.update_schedule.insert_schedule_revision", new=AsyncMock()):
        with patch(
            "app.agent.tools.update_schedule.enqueue_state_sync",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ):
            out = await _update_schedule_impl(
                persona_id="chiwei", content="test", reason="r",
                created_by="chiwei",
            )
    assert "revision_id" in out
    assert "error" not in out


@pytest.mark.asyncio
async def test_enqueue_state_sync_closes_pool_on_error():
    """pool.close must run even if enqueue_job raises, to prevent connection leaks."""
    from app.agent.tools.update_schedule import enqueue_state_sync

    fake_pool = AsyncMock()
    fake_pool.enqueue_job = AsyncMock(side_effect=RuntimeError("redis down"))
    fake_pool.close = AsyncMock()

    fake_settings = type("S", (), {"redis_settings": None, "queue_name": "arq:queue:test-lane"})

    with patch("arq.create_pool", new=AsyncMock(return_value=fake_pool)):
        with patch("app.workers.arq_settings.WorkerSettings", fake_settings):
            with pytest.raises(RuntimeError):
                await enqueue_state_sync(revision_id="sr_xxx")

    fake_pool.close.assert_awaited_once_with(close_connection_pool=True)
    fake_pool.enqueue_job.assert_awaited_once_with(
        "sync_life_state_after_schedule",
        revision_id="sr_xxx",
        _queue_name="arq:queue:test-lane",
    )


@pytest.mark.asyncio
async def test_enqueue_state_sync_closes_pool_on_success():
    """pool.close must run after a successful enqueue and routes to the lane queue."""
    from app.agent.tools.update_schedule import enqueue_state_sync

    fake_pool = AsyncMock()
    fake_pool.enqueue_job = AsyncMock()
    fake_pool.close = AsyncMock()

    fake_settings = type("S", (), {"redis_settings": None, "queue_name": "arq:queue:test-lane"})

    with patch("arq.create_pool", new=AsyncMock(return_value=fake_pool)):
        with patch("app.workers.arq_settings.WorkerSettings", fake_settings):
            await enqueue_state_sync(revision_id="sr_yyy")

    fake_pool.enqueue_job.assert_awaited_once_with(
        "sync_life_state_after_schedule",
        revision_id="sr_yyy",
        _queue_name="arq:queue:test-lane",
    )
    fake_pool.close.assert_awaited_once_with(close_connection_pool=True)
