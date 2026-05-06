"""Tests for app.workers.common — MQ error handler."""

import logging
from unittest.mock import AsyncMock

import pytest

from app.workers.common import mq_error_handler

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
