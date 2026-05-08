"""Durable edge integration tests: RabbitMQ roundtrip + trace/lane context +
consumer-side dedup.

Uses both ``rabbitmq`` (RabbitMQ testcontainer) and ``test_db`` (Postgres
testcontainer) because the consumer's idempotency gate writes a row via
``insert_idempotent``.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import pytest

from app.api.middleware import lane_var, trace_id_var
from app.runtime.data import Data, Key
from app.runtime.durable import publish_durable, start_consumers, stop_consumers
from app.runtime.emit import reset_emit_runtime
from app.runtime.graph import compile_graph
from app.runtime.node import node
from app.runtime.wire import WIRING_REGISTRY, clear_wiring, wire
from tests.runtime.conftest import migrate


class Ping(Data):
    pid: Annotated[str, Key]
    text: str


class LanePing(Data):
    pid: Annotated[str, Key]
    lane: str | None = None


received: list = []
seen_ctx: list = []


@node
async def ping_consumer(p: Ping) -> None:
    received.append(p)
    seen_ctx.append((trace_id_var.get(), lane_var.get()))


@node
async def lane_ping_consumer(p: LanePing) -> None:
    received.append(p)


def setup_function():
    clear_wiring()
    received.clear()
    seen_ctx.clear()
    reset_emit_runtime()


@pytest.fixture
async def durable_env(rabbitmq, inflight_db):
    """Shared setup: migrate Ping + runtime_inflight table + wire + start consumers."""
    await migrate(Ping, inflight_db)

    wire(Ping).to(ping_consumer).durable()
    compile_graph()

    await start_consumers()
    try:
        yield
    finally:
        await stop_consumers()


async def _wait_for(predicate, timeout=5.0):
    """Poll ``predicate`` up to ``timeout`` seconds."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.integration
async def test_durable_roundtrip(durable_env):
    """Publish a Ping on the durable wire; consumer receives it with the
    originating trace contextvar restored from message headers.

    Lane is deliberately left at its contextvar default (``None``) here —
    queue-per-lane routing is a separate concern covered by the
    ``_ensure_lane_queue`` path; this test is about header propagation.
    """
    tok_t = trace_id_var.set("trace-abc")
    try:
        w = next(ws for ws in WIRING_REGISTRY if ws.data_type is Ping)
        await publish_durable(w, ping_consumer, Ping(pid="p1", text="hello"))
    finally:
        trace_id_var.reset(tok_t)

    ok = await _wait_for(lambda: len(received) == 1, timeout=5.0)
    assert ok, "consumer did not receive Ping within 5s"

    assert received[0].pid == "p1"
    assert received[0].text == "hello"

    # trace_id propagated from publisher contextvar -> header -> consumer contextvar.
    trace_id, lane = seen_ctx[0]
    assert trace_id == "trace-abc"
    # Lane wasn't set at publish time, so consumer sees None after restore.
    assert lane is None


@pytest.mark.asyncio
async def test_publish_durable_uses_data_lane_when_context_missing(monkeypatch):
    """Source.mq payloads carry lane in the body, not necessarily in
    contextvars. Durable publish must route by the Data.lane field so
    chat_request_<lane> can fan out to durable_chat_request_*_<lane>.
    """
    wire(LanePing).to(lane_ping_consumer).durable()
    w = next(ws for ws in WIRING_REGISTRY if ws.data_type is LanePing)

    captured = {}

    async def fake_publish(route, body, delay_ms=None, headers=None, lane=None):
        captured.update(
            route=route,
            body=body,
            headers=headers,
            lane=lane,
            delay_ms=delay_ms,
        )

    monkeypatch.setattr("app.runtime.durable.mq.publish", fake_publish)

    tok_t = trace_id_var.set("trace-xyz")
    tok_l = lane_var.set(None)
    try:
        await publish_durable(
            w,
            lane_ping_consumer,
            LanePing(pid="p-lane", lane="feat-flow-parse-5b"),
        )
    finally:
        lane_var.reset(tok_l)
        trace_id_var.reset(tok_t)

    assert captured["body"]["lane"] == "feat-flow-parse-5b"
    assert captured["headers"]["lane"] == "feat-flow-parse-5b"
    assert captured["headers"]["trace_id"] == "trace-xyz"
    assert captured["lane"] == "feat-flow-parse-5b"


@pytest.mark.integration
async def test_durable_idempotent(durable_env):
    """Publish the same Ping twice; only the first is processed —
    ``insert_idempotent`` returns 0 on the second delivery and the
    consumer is not re-invoked.
    """
    w = next(ws for ws in WIRING_REGISTRY if ws.data_type is Ping)
    p = Ping(pid="p-dup", text="once")

    await publish_durable(w, ping_consumer, p)
    await publish_durable(w, ping_consumer, p)

    # Wait for at least one delivery to be observed...
    ok = await _wait_for(lambda: len(received) >= 1, timeout=5.0)
    assert ok, "first delivery never arrived"

    # ...then give the second delivery time to be acked-and-dropped by the
    # idempotency gate. 500ms is plenty — the RabbitMQ hop is sub-ms locally.
    await asyncio.sleep(0.5)

    assert len(received) == 1, (
        f"expected exactly 1 consumer invocation after dedup, got {len(received)}"
    )


@pytest.mark.integration
async def test_start_consumers_is_not_reentrant(durable_env):
    """Second ``start_consumers()`` call without an intervening
    ``stop_consumers()`` must raise instead of registering duplicate
    consumers on the same queue.

    ``durable_env`` already invoked ``start_consumers()`` once during
    setup, so the second call here is the re-entry under test.
    """
    with pytest.raises(RuntimeError, match="already started"):
        await start_consumers()
