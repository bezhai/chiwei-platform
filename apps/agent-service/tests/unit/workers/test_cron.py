"""Tests for app.workers.cron — light reviewer cron wrappers."""

from unittest.mock import AsyncMock, patch

import pytest

from app.workers.cron import (
    cron_memory_reviewer_light_day,
    cron_memory_reviewer_light_night,
)


class TestCronMemoryReviewerLightDay:
    """cron_memory_reviewer_light_day delegates to for_each_persona with label."""

    @pytest.mark.asyncio
    async def test_delegates_to_for_each_persona_with_correct_label(self):
        with (
            patch("app.workers.common.settings") as mock_settings,
            patch("app.workers.cron.for_each_persona", new_callable=AsyncMock) as fep,
        ):
            mock_settings.lane = "prod"
            await cron_memory_reviewer_light_day(None)

        fep.assert_awaited_once()
        _, kwargs = fep.call_args
        assert kwargs.get("label") == "memory_reviewer_light_day"

    @pytest.mark.asyncio
    async def test_skips_in_non_prod_lane(self):
        with (
            patch("app.workers.common.settings") as mock_settings,
            patch("app.workers.cron.for_each_persona", new_callable=AsyncMock) as fep,
        ):
            mock_settings.lane = "dev"
            await cron_memory_reviewer_light_day(None)

        fep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_inner_fn_calls_run_light_review_with_30min_window(self):
        """Verify _run passes window_minutes=30 to run_light_review."""
        captured_fn = None

        async def capture_fep(fn, *, label=""):
            nonlocal captured_fn
            captured_fn = fn

        with (
            patch("app.workers.common.settings") as mock_settings,
            patch("app.workers.cron.for_each_persona", side_effect=capture_fep),
            patch(
                "app.memory.reviewer.light.run_light_review", new_callable=AsyncMock
            ) as rr,
        ):
            mock_settings.lane = "prod"
            await cron_memory_reviewer_light_day(None)

        assert captured_fn is not None
        await captured_fn("test-persona")
        rr.assert_awaited_once_with(persona_id="test-persona", window_minutes=30)


class TestCronMemoryReviewerLightNight:
    """cron_memory_reviewer_light_night delegates to for_each_persona with label."""

    @pytest.mark.asyncio
    async def test_delegates_to_for_each_persona_with_correct_label(self):
        with (
            patch("app.workers.common.settings") as mock_settings,
            patch("app.workers.cron.for_each_persona", new_callable=AsyncMock) as fep,
        ):
            mock_settings.lane = "prod"
            await cron_memory_reviewer_light_night(None)

        fep.assert_awaited_once()
        _, kwargs = fep.call_args
        assert kwargs.get("label") == "memory_reviewer_light_night"

    @pytest.mark.asyncio
    async def test_skips_in_non_prod_lane(self):
        with (
            patch("app.workers.common.settings") as mock_settings,
            patch("app.workers.cron.for_each_persona", new_callable=AsyncMock) as fep,
        ):
            mock_settings.lane = "dev"
            await cron_memory_reviewer_light_night(None)

        fep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_inner_fn_calls_run_light_review_with_60min_window(self):
        """Verify _run passes window_minutes=60 to run_light_review."""
        captured_fn = None

        async def capture_fep(fn, *, label=""):
            nonlocal captured_fn
            captured_fn = fn

        with (
            patch("app.workers.common.settings") as mock_settings,
            patch("app.workers.cron.for_each_persona", side_effect=capture_fep),
            patch(
                "app.memory.reviewer.light.run_light_review", new_callable=AsyncMock
            ) as rr,
        ):
            mock_settings.lane = "prod"
            await cron_memory_reviewer_light_night(None)

        assert captured_fn is not None
        await captured_fn("test-persona")
        rr.assert_awaited_once_with(persona_id="test-persona", window_minutes=60)
