"""runtime_delayed_trigger_{app} queue + internal consumer (Gap 9.1.2/9.3)."""

from __future__ import annotations

from typing import Annotated
from unittest.mock import AsyncMock, patch

import pytest

from app.api.middleware import lane_var, trace_id_var
from app.infra.rabbitmq import (
    DELAYED_TRIGGER_ROUTES,
    KNOWN_APPS_FOR_DELAYED_TRIGGER,
    trigger_route_for,
)
from app.runtime.data import Data, Key
from app.runtime.delayed_trigger import (
    DelayedTriggerEnvelope,
    _runtime_trigger_consumer,
    register_runtime_trigger_wire,
    trigger_route_name_for,
)
from app.runtime.wire import WIRING_REGISTRY, clear_wiring


class _Pong(Data):
    pid: Annotated[str, Key]
    n: int = 0


def setup_function():
    clear_wiring()


class TestTriggerRouteName:
    def test_includes_app_name(self) -> None:
        assert trigger_route_name_for("agent-service") == \
            "runtime_delayed_trigger_agent-service"
        assert trigger_route_name_for("vectorize-worker") == \
            "runtime_delayed_trigger_vectorize-worker"


class TestKnownAppsRoutesRegistered:
    def test_each_known_app_has_route(self) -> None:
        for app in KNOWN_APPS_FOR_DELAYED_TRIGGER:
            r = trigger_route_for(app)
            assert r.queue == trigger_route_name_for(app)
            assert r.lane_fallback is False  # lane envelopes never spill to prod

    def test_routes_appear_in_delayed_trigger_routes(self) -> None:
        names = {r.queue for r in DELAYED_TRIGGER_ROUTES}
        assert names == {
            f"runtime_delayed_trigger_{a}" for a in KNOWN_APPS_FOR_DELAYED_TRIGGER
        }

    def test_unknown_app_raises(self) -> None:
        with pytest.raises(ValueError, match="KNOWN_APPS_FOR_DELAYED_TRIGGER"):
            trigger_route_for("bogus-worker")


class TestRegisterTriggerWire:
    def test_registers_source_mq_to_consumer(self) -> None:
        register_runtime_trigger_wire("agent-service")
        # exactly one wire on DelayedTriggerEnvelope, sourced from the
        # agent-service trigger queue, targeting _runtime_trigger_consumer.
        wires = [w for w in WIRING_REGISTRY if w.data_type is DelayedTriggerEnvelope]
        assert len(wires) == 1
        w = wires[0]
        assert any(s.kind == "mq" and
                   s.params.get("queue") == "runtime_delayed_trigger_agent-service"
                   for s in w.sources)
        assert _runtime_trigger_consumer in w.consumers

    def test_unknown_app_raises(self) -> None:
        with pytest.raises(ValueError, match="KNOWN_APPS_FOR_DELAYED_TRIGGER"):
            register_runtime_trigger_wire("bogus-worker")


class TestTriggerConsumer:
    @pytest.mark.asyncio
    async def test_origin_app_mismatch_logs_and_acks(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        envelope = DelayedTriggerEnvelope(
            origin_app="vectorize-worker",
            origin_lane=None,
            data_type=f"{_Pong.__module__}.{_Pong.__qualname__}",
            payload={"pid": "p1", "n": 1},
            trace_id="t",
        )
        with patch(
            "app.runtime.delayed_trigger.emit", new=AsyncMock()
        ) as mock_emit:
            await _runtime_trigger_consumer(envelope)
        assert "origin_app" in caplog.text
        mock_emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_origin_app_match_calls_emit_with_rebuilt_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        envelope = DelayedTriggerEnvelope(
            origin_app="agent-service",
            origin_lane=None,
            data_type=f"{_Pong.__module__}.{_Pong.__qualname__}",
            payload={"pid": "p1", "n": 7},
            trace_id="t",
        )
        with patch(
            "app.runtime.delayed_trigger.emit", new=AsyncMock()
        ) as mock_emit:
            await _runtime_trigger_consumer(envelope)
        mock_emit.assert_called_once()
        call_arg = mock_emit.call_args.args[0]
        assert isinstance(call_arg, _Pong)
        assert call_arg.pid == "p1"
        assert call_arg.n == 7

    @pytest.mark.asyncio
    async def test_unknown_data_type_logs_warning_and_acks(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        envelope = DelayedTriggerEnvelope(
            origin_app="agent-service",
            origin_lane=None,
            data_type="nonexistent.module.NoSuchType",
            payload={},
            trace_id="t",
        )
        with patch(
            "app.runtime.delayed_trigger.emit", new=AsyncMock()
        ) as mock_emit:
            await _runtime_trigger_consumer(envelope)
        assert "data_type" in caplog.text or "not found" in caplog.text
        mock_emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_envelope_trace_id_lane_propagates_to_emit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        envelope = DelayedTriggerEnvelope(
            origin_app="agent-service",
            origin_lane="feat-x",
            data_type=f"{_Pong.__module__}.{_Pong.__qualname__}",
            payload={"pid": "p1", "n": 1},
            trace_id="orig-trace",
        )
        captured: dict[str, str | None] = {}

        async def fake_emit(data: Data) -> None:
            captured["trace_id"] = trace_id_var.get()
            captured["lane"] = lane_var.get()

        with patch("app.runtime.delayed_trigger.emit", new=fake_emit):
            await _runtime_trigger_consumer(envelope)
        assert captured["trace_id"] == "orig-trace"
        assert captured["lane"] == "feat-x"

    @pytest.mark.asyncio
    async def test_payload_validation_failure_logs_and_acks(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("APP_NAME", "agent-service")
        envelope = DelayedTriggerEnvelope(
            origin_app="agent-service",
            origin_lane=None,
            data_type=f"{_Pong.__module__}.{_Pong.__qualname__}",
            # missing required pid (Annotated[str, Key])
            payload={"n": 1},
            trace_id="t",
        )
        with patch(
            "app.runtime.delayed_trigger.emit", new=AsyncMock()
        ) as mock_emit:
            await _runtime_trigger_consumer(envelope)
        assert "validation" in caplog.text.lower() or "pid" in caplog.text.lower()
        mock_emit.assert_not_called()
