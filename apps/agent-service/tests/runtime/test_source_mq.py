"""MQSource engine loop integration tests.

Covers ``Runtime._source_loop_mq``: consume JSON bodies off a
``lane_queue(queue)`` RabbitMQ queue, decode into the @node's 1-arg Data
type via reflection, invoke the node under restored trace/lane context.

Decode failures (bad JSON / ValidationError) are logged and acked, not
requeued — a poison body must never stall the loop. Business errors in
the @node bubble out of ``message.process(requeue=False)`` so aio-pika
dead-letters the message via the DLX; the loop keeps running on the next
delivery.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import pytest

from app.api.middleware import lane_var, trace_id_var
from app.runtime.data import Data, Key
from app.runtime.emit import reset_emit_runtime
from app.runtime.engine import Runtime
from app.runtime.node import node
from app.runtime.placement import clear_bindings
from app.runtime.source import Source
from app.runtime.wire import clear_wiring, wire


class _Req(Data):
    message_id: Annotated[str, Key]

    class Meta:
        transient = True  # engine only decodes, never persists


# Observed state — per test; setup_function clears.
received: list[_Req] = []
seen_ctx: list[tuple[str | None, str | None]] = []


def setup_function() -> None:
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    received.clear()
    seen_ctx.clear()


async def _run_runtime() -> tuple[Runtime, asyncio.Task]:
    """Start a Runtime with schema migration off; return (rt, task)."""
    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    task = asyncio.create_task(rt.run())
    # Yield so the engine gets past start_consumers() + dispatcher setup
    # before the test starts publishing.
    for _ in range(20):
        if rt._stop_event is not None and rt._source_tasks:
            break
        await asyncio.sleep(0.05)
    return rt, task


async def _stop_runtime(rt: Runtime, task: asyncio.Task) -> None:
    if rt._stop_event is not None:
        rt._stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except TimeoutError:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.integration
async def test_mq_source_consumes_and_invokes_node(rabbitmq):
    """Two JSON bodies on a Source.mq queue each hydrate the target @node
    with a ``_Req`` instance constructed by reflection."""
    from app.infra.rabbitmq import Route, current_lane, lane_queue, mq

    @node
    async def ingest(req: _Req) -> None:
        received.append(req)
        seen_ctx.append((trace_id_var.get(), lane_var.get()))

    wire(_Req).to(ingest).from_(Source.mq("mqsrc_basic"))

    # Declare the queue/binding *before* the engine tries to consume it.
    # Production: lark-server (publisher) + declare_topology own queue
    # creation; the engine is a passive consumer. Matching that ordering
    # in the test keeps the engine's ``get_queue`` passive fetch sound.
    route = Route("mqsrc_basic", "mqsrc_basic.rk")
    await mq.declare_route(route)

    rt, task = await _run_runtime()
    try:
        # Headers exercise trace/lane propagation on the consumer side.
        await mq.publish(
            route, {"message_id": "m1"}, headers={"trace_id": "t-1", "lane": ""}
        )
        await mq.publish(
            route, {"message_id": "m2"}, headers={"trace_id": "t-2", "lane": ""}
        )

        ok = await _wait_until(lambda: len(received) >= 2, timeout=5.0)
        assert ok, f"expected 2 messages, got {len(received)}: {received}"

        ids = sorted(r.message_id for r in received)
        assert ids == ["m1", "m2"]

        # Trace context propagated via headers; lane sent as "" (no lane)
        # becomes None by the defensive coercion borrowed from durable.py.
        traces = sorted(t for t, _ in seen_ctx)
        assert traces == ["t-1", "t-2"]
        assert all(lane is None for _, lane in seen_ctx)

        # Sanity check: the engine is consuming the lane-aware queue name.
        assert lane_queue("mqsrc_basic", current_lane()) == "mqsrc_basic"
    finally:
        await _stop_runtime(rt, task)


@pytest.mark.integration
async def test_mq_source_ignores_decode_failures(rabbitmq, caplog):
    """Bad-JSON body is acked (no poison loop) and the next valid body is
    still processed. Runtime stays healthy."""
    from app.infra.rabbitmq import Route, mq

    @node
    async def ingest(req: _Req) -> None:
        received.append(req)

    wire(_Req).to(ingest).from_(Source.mq("mqsrc_bad"))

    route = Route("mqsrc_bad", "mqsrc_bad.rk")
    await mq.declare_route(route)

    rt, task = await _run_runtime()
    try:
        # Publish a bad frame directly through the exchange so the engine
        # sees raw bytes, not a dict-encoded body.
        assert mq._exchange is not None  # type: ignore[attr-defined]
        from aio_pika import DeliveryMode, Message

        await mq._exchange.publish(  # type: ignore[attr-defined]
            Message(
                body=b"not json",
                delivery_mode=DeliveryMode.PERSISTENT,
                content_type="application/json",
            ),
            routing_key=route.rk,
        )
        # Valid message — should process despite the earlier bad one.
        await mq.publish(route, {"message_id": "ok"})

        with caplog.at_level(logging.WARNING, logger="app.runtime.engine"):
            ok = await _wait_until(lambda: len(received) >= 1, timeout=5.0)
        assert ok, "valid message never reached the node"
        assert received[0].message_id == "ok"
        # No requeue storm: loop is alive, we didn't see exponential blow-up.
        assert len(received) == 1

        # decode-failure warning was logged (engine namespace).
        decode_warnings = [
            r for r in caplog.records if "decode" in r.getMessage().lower()
        ]
        assert decode_warnings, (
            f"expected a decode-failure warning; got records: "
            f"{[r.getMessage() for r in caplog.records]}"
        )
    finally:
        await _stop_runtime(rt, task)


@pytest.mark.integration
async def test_mq_source_lane_aware_queue_name(rabbitmq, monkeypatch):
    """When a lane is active, the engine consumes from the lane-scoped
    queue (``<base>_<lane>``), matching the lane-aware MQ consumer contract."""
    from app.infra.rabbitmq import Route, mq

    # Simulate non-prod lane for this process.
    monkeypatch.setenv("LANE", "mqtest")

    @node
    async def ingest(req: _Req) -> None:
        received.append(req)

    wire(_Req).to(ingest).from_(Source.mq("mqsrc_lane"))

    route = Route("mqsrc_lane", "mqsrc_lane")
    # declare_route uses current_lane() -> "mqtest" and sets up the
    # lane-scoped binding + TTL fallback automatically.
    await mq.declare_route(route)

    rt, task = await _run_runtime()
    try:
        await mq.publish(route, {"message_id": "lane-1"})

        ok = await _wait_until(lambda: len(received) >= 1, timeout=5.0)
        assert ok, "lane-scoped queue delivery never arrived"
        assert received[0].message_id == "lane-1"
    finally:
        await _stop_runtime(rt, task)


@pytest.mark.integration
async def test_mq_source_business_error_does_not_poison_loop(rabbitmq):
    """A raising @node must not stall the loop: aio-pika ack-nacks the bad
    message (requeue=False -> DLX), and subsequent messages keep flowing."""
    from app.infra.rabbitmq import Route, mq

    call_count = {"n": 0}

    @node
    async def ingest(req: _Req) -> None:
        call_count["n"] += 1
        if req.message_id == "boom":
            raise RuntimeError("boom")
        received.append(req)

    wire(_Req).to(ingest).from_(Source.mq("mqsrc_err"))

    route = Route("mqsrc_err", "mqsrc_err.rk")
    await mq.declare_route(route)

    rt, task = await _run_runtime()
    try:
        await mq.publish(route, {"message_id": "boom"})
        await mq.publish(route, {"message_id": "good"})

        ok = await _wait_until(lambda: len(received) >= 1, timeout=5.0)
        assert ok, "good message never arrived after poison boom"
        assert received[0].message_id == "good"
        # boom was delivered exactly once (requeue=False), good once.
        # Allow small margin in case of a single redelivery cycle.
        assert call_count["n"] <= 3
    finally:
        await _stop_runtime(rt, task)


# ---------------------------------------------------------------------------
# Wire-validation: mq-source wires must target a single 1-Data-arg @node.
# ---------------------------------------------------------------------------


async def test_mq_wire_requires_exactly_one_consumer(rabbitmq):
    """compile_graph (or Runtime startup) rejects a Source.mq wire that
    fans out to multiple consumers — engine can't pick a single decode
    target."""
    from app.runtime.graph import GraphError, compile_graph

    @node
    async def one(req: _Req) -> None: ...

    @node
    async def two(req: _Req) -> None: ...

    wire(_Req).to(one, two).from_(Source.mq("mqsrc_multi"))

    with pytest.raises(GraphError, match="Source.mq"):
        compile_graph()
