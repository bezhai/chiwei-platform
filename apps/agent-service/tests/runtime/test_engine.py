"""Runtime engine tests: cron/interval source loops + app-scoped consumer filter.

Focuses on Runtime-level behavior that can't be exercised by the
per-module unit tests:

  - a configured ``Source.interval`` actually fires the wired consumer;
  - the runtime routes emits through ``emit()`` (so in-process
    consumers see them without a RabbitMQ roundtrip);
  - ``nodes_for_app`` filtering keeps this-app runtimes from starting
    source loops for other-app wires;
  - the cron/interval payload contract is enforced at startup (missing
    ``ts`` field raises rather than silently dropping ticks).
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from app.runtime.data import Data, Key
from app.runtime.emit import reset_emit_runtime
from app.runtime.engine import Runtime
from app.runtime.node import node
from app.runtime.placement import bind, clear_bindings
from app.runtime.source import Source
from app.runtime.wire import clear_wiring, wire


class Tick(Data):
    ts: Annotated[str, Key]


class OtherTick(Data):
    ts: Annotated[str, Key]


class BadTick(Data):
    """Lacks a ``ts`` field — cron/interval payload construction must fail."""

    tid: Annotated[str, Key]


agent_counter: list[Tick] = []
worker_counter: list[OtherTick] = []


@node
async def count_ticks(t: Tick) -> None:
    agent_counter.append(t)


@node
async def count_other_ticks(t: OtherTick) -> None:
    worker_counter.append(t)


@node
async def bad_consumer(b: BadTick) -> None:  # pragma: no cover - never fires
    raise AssertionError("bad_consumer should not be reachable")


def setup_function() -> None:
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    agent_counter.clear()
    worker_counter.clear()


async def _run_for(runtime: Runtime, seconds: float) -> None:
    """Start ``runtime.run()`` as a task, wait ``seconds``, then cancel."""
    task = asyncio.create_task(runtime.run())
    try:
        await asyncio.sleep(seconds)
    finally:
        # Trigger the engine's internal stop path.
        if runtime._stop_event is not None:
            runtime._stop_event.set()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def test_runtime_fires_interval_consumer() -> None:
    """Runtime with an ``interval`` source drives the wired consumer
    repeatedly while it's running.

    We pick 100ms period and watch for ~3 ticks inside 500ms. Asserting
    ``>= 2`` keeps the test robust to GC pauses without losing signal.
    """
    wire(Tick).to(count_ticks).from_(Source.interval(seconds=0.1))

    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    await _run_for(rt, seconds=0.5)

    assert len(agent_counter) >= 2, (
        f"expected >=2 ticks in 500ms at 100ms interval; got {len(agent_counter)}"
    )


async def test_runtime_skips_other_app_source_loops() -> None:
    """A wire whose consumer is bound to another app must NOT have its
    source loop started in this app's runtime.
    """
    wire(Tick).to(count_ticks).from_(Source.interval(seconds=0.05))
    wire(OtherTick).to(count_other_ticks).from_(Source.interval(seconds=0.05))
    bind(count_other_ticks).to_app("vectorize-worker")
    # count_ticks stays unbound -> default "agent-service".

    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    await _run_for(rt, seconds=0.3)

    assert len(agent_counter) >= 2, (
        "agent-service wire's consumer should fire on its interval source"
    )
    assert worker_counter == [], (
        "vectorize-worker wire must not fire in the agent-service runtime; "
        f"got {len(worker_counter)} unexpected invocations"
    )


async def test_runtime_rejects_payload_without_ts_field() -> None:
    """A cron/interval source wired to a Data class without a ``ts``
    field must raise at first tick — silent drops would hide misconfig.
    """
    wire(BadTick).to(bad_consumer).from_(Source.interval(seconds=0.05))

    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)

    task = asyncio.create_task(rt.run())
    # Give the source loop time to fire once; it should blow up.
    try:
        # The source loop runs in a child task and raises there, not at
        # run() level. Wait for the task to reflect the failure.
        await asyncio.sleep(0.25)
        # Find the source task and verify it errored.
        assert rt._source_tasks, "runtime should have created a source task"
        src_task = rt._source_tasks[0]
        # Wait for it to complete (it will have errored).
        for _ in range(20):
            if src_task.done():
                break
            await asyncio.sleep(0.05)
        assert src_task.done(), "source task should have exited on RuntimeError"
        exc = src_task.exception()
        assert isinstance(exc, RuntimeError), (
            f"expected RuntimeError from source loop, got {exc!r}"
        )
        assert "requires a 'ts: str' field" in str(exc)
    finally:
        if rt._stop_event is not None:
            rt._stop_event.set()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
