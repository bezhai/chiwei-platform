"""Tests for worker error handling decorators"""

import logging
from unittest.mock import AsyncMock

import pytest

from app.workers.error_handling import cron_error_handler, mq_error_handler


class TestCronErrorHandler:
    """cron_error_handler 装饰器测试"""

    @pytest.mark.asyncio
    async def test_success_returns_result(self):
        @cron_error_handler()
        async def happy_cron(ctx):
            return "done"

        result = await happy_cron(None)
        assert result == "done"

    @pytest.mark.asyncio
    async def test_exception_returns_none(self, caplog):
        @cron_error_handler()
        async def failing_cron(ctx):
            raise RuntimeError("boom")

        with caplog.at_level(logging.ERROR):
            result = await failing_cron(None)

        assert result is None
        assert "Cron job failing_cron failed" in caplog.text

    @pytest.mark.asyncio
    async def test_preserves_function_name(self):
        @cron_error_handler()
        async def my_cron_job(ctx):
            pass

        assert my_cron_job.__name__ == "my_cron_job"

    @pytest.mark.asyncio
    async def test_does_not_interrupt_scheduler(self):
        """连续调用：第一次失败不影响第二次"""
        call_count = 0

        @cron_error_handler()
        async def flaky_cron(ctx):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first call fails")
            return "second ok"

        r1 = await flaky_cron(None)
        r2 = await flaky_cron(None)
        assert r1 is None
        assert r2 == "second ok"


class TestMqErrorHandler:
    """mq_error_handler 装饰器测试"""

    @pytest.mark.asyncio
    async def test_success_returns_result(self):
        @mq_error_handler()
        async def happy_handler(message):
            return "processed"

        result = await happy_handler(AsyncMock())
        assert result == "processed"

    @pytest.mark.asyncio
    async def test_exception_nacks_message(self, caplog):
        mock_message = AsyncMock()
        mock_message.nack = AsyncMock()

        @mq_error_handler()
        async def failing_handler(message):
            raise ValueError("bad payload")

        with caplog.at_level(logging.ERROR):
            result = await failing_handler(mock_message)

        assert result is None
        mock_message.nack.assert_awaited_once_with(requeue=False)
        assert "MQ handler failing_handler failed" in caplog.text

    @pytest.mark.asyncio
    async def test_exception_without_nack_method(self, caplog):
        """message 没有 nack 方法时不 crash"""

        class SimpleMessage:
            pass

        @mq_error_handler()
        async def failing_handler(message):
            raise RuntimeError("oops")

        with caplog.at_level(logging.ERROR):
            result = await failing_handler(SimpleMessage())

        assert result is None
        assert "MQ handler failing_handler failed" in caplog.text

    @pytest.mark.asyncio
    async def test_preserves_function_name(self):
        @mq_error_handler()
        async def my_mq_handler(message):
            pass

        assert my_mq_handler.__name__ == "my_mq_handler"

    @pytest.mark.asyncio
    async def test_passes_extra_args(self):
        @mq_error_handler()
        async def handler_with_args(message, extra, key=None):
            return f"{extra}-{key}"

        result = await handler_with_args(AsyncMock(), "val", key="k")
        assert result == "val-k"
