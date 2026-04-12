"""Tests for app.workers.common — shared worker utilities."""

import logging
from unittest.mock import AsyncMock, patch

import pytest

from app.workers.common import (
    cron_error_handler,
    for_each_persona,
    mq_error_handler,
    prod_only,
)

# ---------------------------------------------------------------------------
# for_each_persona
# ---------------------------------------------------------------------------


class TestForEachPersona:
    """for_each_persona iterates all personas with error isolation."""

    @pytest.mark.asyncio
    async def test_calls_fn_for_every_persona(self):
        called = []

        async def _fn(pid: str) -> None:
            called.append(pid)

        fake_session = AsyncMock()
        with (
            patch("app.workers.common.get_session") as mock_gs,
            patch(
                "app.workers.common.list_all_persona_ids", return_value=["a", "b", "c"]
            ),
        ):
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=fake_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)
            await for_each_persona(_fn, label="test")

        assert called == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_one_failure_does_not_stop_others(self, caplog):
        called = []

        async def _fn(pid: str) -> None:
            if pid == "b":
                raise RuntimeError("boom")
            called.append(pid)

        fake_session = AsyncMock()
        with (
            patch("app.workers.common.get_session") as mock_gs,
            patch(
                "app.workers.common.list_all_persona_ids",
                return_value=["a", "b", "c"],
            ),
            caplog.at_level(logging.ERROR),
        ):
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=fake_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)
            await for_each_persona(_fn, label="test")

        # a and c still ran despite b failing
        assert called == ["a", "c"]
        assert "boom" in caplog.text

    @pytest.mark.asyncio
    async def test_empty_persona_list(self):
        called = []

        async def _fn(pid: str) -> None:
            called.append(pid)

        fake_session = AsyncMock()
        with (
            patch("app.workers.common.get_session") as mock_gs,
            patch("app.workers.common.list_all_persona_ids", return_value=[]),
        ):
            mock_gs.return_value.__aenter__ = AsyncMock(return_value=fake_session)
            mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)
            await for_each_persona(_fn, label="empty")

        assert called == []


# ---------------------------------------------------------------------------
# prod_only
# ---------------------------------------------------------------------------


class TestProdOnly:
    """prod_only decorator skips non-prod lanes."""

    @pytest.mark.asyncio
    async def test_runs_in_prod(self):
        @prod_only
        async def my_cron(ctx):
            return "ran"

        with patch("app.workers.common.settings") as mock_settings:
            mock_settings.lane = None
            result = await my_cron(None)
        assert result == "ran"

    @pytest.mark.asyncio
    async def test_runs_when_lane_is_prod(self):
        @prod_only
        async def my_cron(ctx):
            return "ran"

        with patch("app.workers.common.settings") as mock_settings:
            mock_settings.lane = "prod"
            result = await my_cron(None)
        assert result == "ran"

    @pytest.mark.asyncio
    async def test_skips_in_dev_lane(self):
        @prod_only
        async def my_cron(ctx):
            return "ran"

        with patch("app.workers.common.settings") as mock_settings:
            mock_settings.lane = "dev"
            result = await my_cron(None)
        assert result is None

    @pytest.mark.asyncio
    async def test_preserves_function_name(self):
        @prod_only
        async def my_cron_job(ctx):
            pass

        assert my_cron_job.__name__ == "my_cron_job"


# ---------------------------------------------------------------------------
# cron_error_handler
# ---------------------------------------------------------------------------


class TestCronErrorHandler:
    """cron_error_handler catches exceptions without crashing the scheduler."""

    @pytest.mark.asyncio
    async def test_success_returns_result(self):
        @cron_error_handler()
        async def happy(ctx):
            return "ok"

        assert await happy(None) == "ok"

    @pytest.mark.asyncio
    async def test_exception_returns_none(self, caplog):
        @cron_error_handler()
        async def failing(ctx):
            raise RuntimeError("boom")

        with caplog.at_level(logging.ERROR):
            result = await failing(None)
        assert result is None
        assert "Cron job failing failed" in caplog.text

    @pytest.mark.asyncio
    async def test_preserves_name(self):
        @cron_error_handler()
        async def my_job(ctx):
            pass

        assert my_job.__name__ == "my_job"


# ---------------------------------------------------------------------------
# mq_error_handler
# ---------------------------------------------------------------------------


class TestMqErrorHandler:
    """mq_error_handler nacks on exception, passes through on success."""

    @pytest.mark.asyncio
    async def test_success(self):
        @mq_error_handler()
        async def handler(msg):
            return "done"

        assert await handler(AsyncMock()) == "done"

    @pytest.mark.asyncio
    async def test_exception_nacks(self, caplog):
        mock_msg = AsyncMock()
        mock_msg.nack = AsyncMock()

        @mq_error_handler()
        async def handler(msg):
            raise ValueError("bad")

        with caplog.at_level(logging.ERROR):
            result = await handler(mock_msg)

        assert result is None
        mock_msg.nack.assert_awaited_once_with(requeue=False)

    @pytest.mark.asyncio
    async def test_no_nack_method(self, caplog):
        class SimpleMsg:
            pass

        @mq_error_handler()
        async def handler(msg):
            raise RuntimeError("oops")

        with caplog.at_level(logging.ERROR):
            result = await handler(SimpleMsg())
        assert result is None
