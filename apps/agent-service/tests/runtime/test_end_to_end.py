"""End-to-end smoke: emit -> durable RabbitMQ -> consume -> insert_idempotent -> query.

Validates that the individually-unit-tested Phase 0 pieces actually integrate.
Covers in one test:
  - @node registration + compile_graph()
  - wire(T).to(consumer).durable()
  - emit() routing to publish_durable (NOT a direct publish_durable call)
  - real RabbitMQ publish + consume (testcontainer)
  - consumer-side insert_idempotent writing to real Postgres (testcontainer)
  - query(T).where(...).all() reading back what the consumer just persisted

Each unit test verifies one layer; this test verifies the seams.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import pytest

from app.api.middleware import trace_id_var
from app.runtime.data import Data, DedupKey, Key
from app.runtime.durable import start_consumers, stop_consumers
from app.runtime.emit import emit, reset_emit_runtime
from app.runtime.graph import compile_graph
from app.runtime.node import node
from app.runtime.query import query
from app.runtime.wire import clear_wiring, wire
from tests.runtime.conftest import migrate


class SmokeMsg(Data):
    smoke_id: Annotated[str, Key, DedupKey]
    text: str


received: list[SmokeMsg] = []


@node
async def smoke_consumer(m: SmokeMsg) -> None:
    received.append(m)


def setup_function():
    clear_wiring()
    received.clear()
    reset_emit_runtime()


@pytest.fixture
async def smoke_env(rabbitmq, test_db):
    await migrate(SmokeMsg, test_db)
    wire(SmokeMsg).to(smoke_consumer).durable()
    compile_graph()
    await start_consumers()
    try:
        yield
    finally:
        await stop_consumers()


async def _wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.integration
async def test_emit_to_durable_consumer_persists_and_query_reads(smoke_env):
    """Full Phase 0 roundtrip: emit -> RabbitMQ -> consumer -> pg insert -> query reads it."""
    tok = trace_id_var.set("smoke-trace-1")
    try:
        await emit(SmokeMsg(smoke_id="sm-1", text="hello from emit"))
    finally:
        trace_id_var.reset(tok)

    ok = await _wait_for(lambda: len(received) == 1, timeout=5.0)
    assert ok, f"consumer never fired within 5s; received={received!r}"

    # Consumer-side insert_idempotent committed the row; query(T) must find it.
    rows = await query(SmokeMsg).where(smoke_id="sm-1").all()
    assert len(rows) == 1, f"query returned {len(rows)} rows, expected 1"
    assert rows[0].smoke_id == "sm-1"
    assert rows[0].text == "hello from emit"
