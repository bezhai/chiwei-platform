"""Tests for Phase 2 Sink dispatch (emit -> mq.publish via SinkSpec)."""
from __future__ import annotations

from typing import Annotated
from unittest.mock import AsyncMock, patch

import pytest

from app.runtime import Data, Key, Sink, node, wire
from app.runtime.emit import emit, reset_emit_runtime
from app.runtime.placement import bind, clear_bindings
from app.runtime.wire import clear_wiring


# Module-level Data classes so @node's get_type_hints() can resolve annotations.


class _RecallProbe(Data):
    session_id: Annotated[str, Key]
    chat_id: str

    class Meta:
        transient = True


class _MixData(Data):
    session_id: Annotated[str, Key]

    class Meta:
        transient = True


_mix_consumer_calls: list = []


@node
async def _consume_mix(req: _MixData) -> None:
    _mix_consumer_calls.append(req)
    return None


@pytest.fixture(autouse=True)
def _reset_runtime():
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    _mix_consumer_calls.clear()
    yield
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    _mix_consumer_calls.clear()


@pytest.mark.asyncio
async def test_emit_dispatches_to_sink_mq_using_route_from_all_routes(monkeypatch):
    """emit Data 触发 wire(Data).to(Sink.mq("recall")) → mq.publish(RECALL, body)."""
    wire(_RecallProbe).to(Sink.mq("recall"))

    monkeypatch.setenv("APP_NAME", "agent-service")

    fake_publish = AsyncMock()
    with patch("app.runtime.sink_dispatch.mq.publish", fake_publish):
        data = _RecallProbe(session_id="s1", chat_id="c1")
        await emit(data)

    assert fake_publish.await_count == 1
    args, _kwargs = fake_publish.await_args
    route, body = args[0], args[1]
    assert route.queue == "recall"
    assert route.rk == "action.recall"
    assert body["session_id"] == "s1"
    assert body["chat_id"] == "c1"


@pytest.mark.asyncio
async def test_emit_dispatches_to_sink_alongside_consumer(monkeypatch):
    """同一 Data 上 wire 到 sink 和 consumer，两者都触发。"""
    wire(_MixData).to(_consume_mix)
    wire(_MixData).to(Sink.mq("recall"))
    bind(_consume_mix).to_app("agent-service")
    monkeypatch.setenv("APP_NAME", "agent-service")

    fake_publish = AsyncMock()
    with patch("app.runtime.sink_dispatch.mq.publish", fake_publish):
        await emit(_MixData(session_id="s1"))

    assert len(_mix_consumer_calls) == 1
    assert fake_publish.await_count == 1


@pytest.mark.asyncio
async def test_route_by_queue_returns_matching_route():
    """_route_by_queue 通过 queue 名查 ALL_ROUTES，找到返回 Route，找不到返回 None."""
    from app.runtime.sink_dispatch import _route_by_queue

    r = _route_by_queue("recall")
    assert r is not None
    assert r.queue == "recall"
    assert r.rk == "action.recall"

    assert _route_by_queue("not_in_all_routes") is None
