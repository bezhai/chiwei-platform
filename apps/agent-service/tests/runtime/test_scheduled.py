"""In-process scheduled task pool (Gap 9.2 best_effort)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.runtime.scheduled import (
    SCHEDULED_TASKS,
    cancel_all_scheduled,
    schedule_after,
)


class TestScheduledTaskPool:
    @pytest.mark.asyncio
    async def test_runs_callable_after_delay(self) -> None:
        cancel_all_scheduled()
        called = asyncio.Event()

        async def fire() -> None:
            called.set()

        await schedule_after(0.05, fire)
        await asyncio.wait_for(called.wait(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_task_added_to_pool(self) -> None:
        cancel_all_scheduled()

        async def fire() -> None:
            await asyncio.sleep(10)

        task = await schedule_after(10, fire)
        try:
            assert task in SCHEDULED_TASKS
        finally:
            task.cancel()
            cancel_all_scheduled()

    @pytest.mark.asyncio
    async def test_completed_task_removed_from_pool(self) -> None:
        cancel_all_scheduled()
        ran = asyncio.Event()

        async def fire() -> None:
            ran.set()

        task = await schedule_after(0.01, fire)
        await asyncio.wait_for(ran.wait(), timeout=0.5)
        await asyncio.sleep(0.05)
        assert task not in SCHEDULED_TASKS

    @pytest.mark.asyncio
    async def test_cancel_all_cancels_pending(self) -> None:
        cancel_all_scheduled()

        async def fire() -> None:
            await asyncio.sleep(10)

        t1 = await schedule_after(10, fire)
        t2 = await schedule_after(10, fire)
        n = cancel_all_scheduled()
        # Yield twice so the cancellation propagates and both task done
        # callbacks have a chance to run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert n == 2
        assert t1.cancelled() and t2.cancelled()
        assert not SCHEDULED_TASKS

    @pytest.mark.asyncio
    async def test_callable_exceptions_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cancel_all_scheduled()
        ran = asyncio.Event()

        async def boom() -> None:
            try:
                raise RuntimeError("scheduled boom")
            finally:
                ran.set()

        with caplog.at_level(logging.ERROR, logger="app.runtime.scheduled"):
            await schedule_after(0.01, boom)
            await asyncio.wait_for(ran.wait(), timeout=0.5)
            await asyncio.sleep(0.05)
        assert "scheduled boom" in caplog.text or "RuntimeError" in caplog.text
