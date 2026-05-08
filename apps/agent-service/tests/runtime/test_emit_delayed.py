"""emit_delayed / emit_at API (Gap 9.1)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated
from unittest.mock import AsyncMock, patch

import pytest

from app.api.middleware import lane_var, trace_id_var
from app.runtime.data import Data, Key
from app.runtime.emit import emit_at, emit_delayed
from app.runtime.scheduled import SCHEDULED_TASKS, cancel_all_scheduled
from app.runtime.wire import clear_wiring


class _Pong(Data):
    pid: Annotated[str, Key]
    n: int = 0


def setup_function():
    clear_wiring()
    cancel_all_scheduled()


class TestEmitDelayedValidation:
    @pytest.mark.asyncio
    async def test_invalid_durability_raises(self) -> None:
        with pytest.raises(ValueError, match="durability"):
            await emit_delayed(_Pong(pid="p1"), delay_ms=0, durability="weird")

    @pytest.mark.asyncio
    async def test_negative_delay_clamps_to_zero_calls_emit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        with patch(
            "app.runtime.emit.emit", new=AsyncMock()
        ) as mock_emit:
            await emit_delayed(_Pong(pid="p1"), delay_ms=-100)
        mock_emit.assert_called_once()

    @pytest.mark.asyncio
    async def test_delay_exceeds_x_delay_max_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        with pytest.raises(ValueError, match="x-delay"):
            await emit_delayed(_Pong(pid="p1"), delay_ms=2_500_000_000)


class TestEmitDelayedZeroDelay:
    @pytest.mark.asyncio
    async def test_zero_delay_calls_emit_directly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        with patch(
            "app.runtime.emit.emit", new=AsyncMock()
        ) as mock_emit:
            await emit_delayed(_Pong(pid="p1", n=1), delay_ms=0)
        mock_emit.assert_called_once()
        assert mock_emit.call_args.args[0].n == 1


class TestEmitDelayedDurable:
    @pytest.mark.asyncio
    async def test_publishes_envelope_to_trigger_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        from app.infra.rabbitmq import mq

        with patch.object(
            mq, "publish_with_confirm",
            new=AsyncMock(return_value=True),
        ) as mock_pub:
            await emit_delayed(_Pong(pid="p1", n=1), delay_ms=5000)
        mock_pub.assert_called_once()
        args, kwargs = mock_pub.call_args
        route = args[0]
        body = args[1]
        assert route.queue == "runtime_delayed_trigger_agent-service"
        assert kwargs["delay_ms"] == 5000
        assert body["origin_app"] == "agent-service"
        assert body["data_type"].endswith("_Pong")
        assert body["payload"] == {"pid": "p1", "n": 1}

    @pytest.mark.asyncio
    async def test_publish_failure_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        from app.infra.rabbitmq import mq

        with patch.object(
            mq, "publish_with_confirm",
            new=AsyncMock(return_value=False),
        ):
            with pytest.raises(RuntimeError, match="EmitDelayedDispatchFailed"):
                await emit_delayed(_Pong(pid="p1"), delay_ms=1000)

    @pytest.mark.asyncio
    async def test_envelope_carries_lane_and_trace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.infra.rabbitmq import mq

        monkeypatch.setenv("APP_NAME", "agent-service")
        l_tok = lane_var.set("feat-x")
        t_tok = trace_id_var.set("trace-1")
        try:
            with patch.object(
                mq, "publish_with_confirm",
                new=AsyncMock(return_value=True),
            ) as mock_pub:
                await emit_delayed(_Pong(pid="p1"), delay_ms=1000)
        finally:
            lane_var.reset(l_tok)
            trace_id_var.reset(t_tok)
        body = mock_pub.call_args.args[1]
        assert body["origin_lane"] == "feat-x"
        assert body["trace_id"] == "trace-1"

    @pytest.mark.asyncio
    async def test_unknown_app_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APP_NAME", "bogus-worker")
        with pytest.raises(RuntimeError, match="KNOWN_APPS_FOR_DELAYED_TRIGGER"):
            await emit_delayed(_Pong(pid="p1"), delay_ms=1000)


class TestEmitDelayedBestEffort:
    @pytest.mark.asyncio
    async def test_best_effort_uses_schedule_after(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cancel_all_scheduled()
        with patch(
            "app.runtime.emit.emit", new=AsyncMock()
        ) as mock_emit:
            await emit_delayed(
                _Pong(pid="p1", n=4), delay_ms=50, durability="best_effort",
            )
            assert len(SCHEDULED_TASKS) == 1
            await asyncio.sleep(0.1)
        mock_emit.assert_called_once()
        assert mock_emit.call_args.args[0].n == 4


class TestEmitAt:
    @pytest.mark.asyncio
    async def test_converts_to_delay_ms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called_ms: list[int] = []

        async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
            called_ms.append(delay_ms)

        with patch("app.runtime.emit.emit_delayed", new=fake_emit_delayed):
            when = datetime.now(UTC) + timedelta(seconds=10)
            await emit_at(_Pong(pid="p1"), when=when)
        assert 9_500 < called_ms[0] < 10_500

    @pytest.mark.asyncio
    async def test_past_when_uses_zero_delay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called_ms: list[int] = []

        async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
            called_ms.append(delay_ms)

        with patch("app.runtime.emit.emit_delayed", new=fake_emit_delayed):
            when = datetime.now(UTC) - timedelta(seconds=5)
            await emit_at(_Pong(pid="p1"), when=when)
        assert called_ms[0] == 0

    @pytest.mark.asyncio
    async def test_naive_datetime_treated_as_utc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called_ms: list[int] = []

        async def fake_emit_delayed(data, *, delay_ms, durability="durable"):
            called_ms.append(delay_ms)

        with patch("app.runtime.emit.emit_delayed", new=fake_emit_delayed):
            when = datetime.utcnow() + timedelta(seconds=5)  # naive
            await emit_at(_Pong(pid="p1"), when=when)
        # Should have produced ~5s delay; allow generous tolerance for
        # second-boundary skew on slow CI.
        assert 4_000 < called_ms[0] < 6_000
