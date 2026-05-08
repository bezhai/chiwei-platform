"""wire(...).durable().retry() handler behavior (Gap 7.2 integration)."""

from __future__ import annotations

import asyncio
from typing import Annotated

import pytest
from sqlalchemy import text

from app.api.middleware import trace_id_var
from app.data.session import get_session
from app.runtime.data import Data, Key
from app.runtime.durable import publish_durable, start_consumers, stop_consumers
from app.runtime.emit import reset_emit_runtime
from app.runtime.graph import compile_graph
from app.runtime.node import node
from app.runtime.wire import WIRING_REGISTRY, clear_wiring, wire
from tests.runtime.conftest import migrate


class RetryJob(Data):
    jid: Annotated[str, Key]


# Module-level state observed by the consumer; setup_function clears.
attempt_counter: list[int] = []


@node
async def flaky_consumer(j: RetryJob) -> None:
    attempt_counter.append(1)
    raise RuntimeError("drill-injected failure")


def setup_function():
    clear_wiring()
    attempt_counter.clear()
    reset_emit_runtime()


async def _wait_for(predicate, timeout=10.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def _inflight_row(jid: str) -> dict | None:
    async with get_session() as s:
        r = await s.execute(text(
            "SELECT state, attempts, last_error "
            "FROM runtime_inflight "
            "WHERE edge_id LIKE 'RetryJob::%' AND idempotent_key=("
            "  SELECT dedup_hash FROM data_retry_job WHERE jid=:j"
            ")"
        ), {"j": jid})
        m = r.mappings().first()
        return dict(m) if m is not None else None


@pytest.fixture
async def retry_env(rabbitmq, inflight_db):
    """Wire RetryJob → flaky_consumer with .durable().retry(n=3)."""
    await migrate(RetryJob, inflight_db)

    wire(RetryJob).to(flaky_consumer).durable().retry(
        n=3, backoff="exponential", base_delay_ms=100, max_delay_ms=1000,
    )
    compile_graph()

    await start_consumers()
    try:
        yield
    finally:
        await stop_consumers()


@pytest.mark.integration
async def test_retry_n_attempts_then_dlq(retry_env):
    """Consumer that always raises: handler republishes attempts up to n,
    then lets the final failure DLQ. Inflight row terminates at
    state=failed, attempts=n.
    """
    w = next(ws for ws in WIRING_REGISTRY if ws.data_type is RetryJob)

    tok = trace_id_var.set("retry-drill")
    try:
        await publish_durable(w, flaky_consumer, RetryJob(jid="j1"))
    finally:
        trace_id_var.reset(tok)

    # n=3: first delivery + 2 retries → 3 total consumer invocations.
    ok = await _wait_for(lambda: len(attempt_counter) >= 3, timeout=10.0)
    assert ok, (
        f"expected >=3 consumer invocations after retry chain, "
        f"got {len(attempt_counter)}"
    )

    # Settle: give the final DLQ nack time to land + the inflight UPDATE
    # to commit before assertion.
    await asyncio.sleep(0.5)

    row = await _inflight_row("j1")
    assert row is not None, "inflight row never created"
    assert row["state"] == "failed"
    assert row["attempts"] == 3
    assert "drill-injected failure" in (row["last_error"] or "")
