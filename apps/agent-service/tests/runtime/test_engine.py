"""Runtime engine tests: how the engine turns interval wires into clocks.

Covers what only the engine does:

  - a configured ``Source.interval`` fires the wired consumer through ``emit()``
    (so in-process consumers see it without a RabbitMQ roundtrip);
  - ``nodes_for_app`` filtering keeps this-app runtimes from starting
    source loops for other-app wires;
  - an app nobody bound a node to fails to start its loops;
  - the engine's own lane flag, and its payload construction: a Data without a
    ``ts`` field fails inside the clock loop, which is fatal.

How a clock ticks (fire-and-forget, errors, the watchdog, trace ids, lane gating) is
:mod:`app.runtime.clock`, shared with the plugin host and tested through it in
``tests/host/test_clocks.py``.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import pytest

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


agent_counter: list[Tick] = []
worker_counter: list[OtherTick] = []


@node
async def count_ticks(t: Tick) -> None:
    agent_counter.append(t)


@node
async def count_other_ticks(t: OtherTick) -> None:
    worker_counter.append(t)


def setup_function() -> None:
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    agent_counter.clear()
    worker_counter.clear()


async def _run_for(runtime: Runtime, seconds: float) -> None:
    """Start the runtime's source loops, wait ``seconds``, then stop them."""
    await runtime.start_source_loops()
    try:
        await asyncio.sleep(seconds)
    finally:
        await runtime.stop_source_loops()


async def test_runtime_fires_interval_consumer() -> None:
    """Runtime with an ``interval`` source drives the wired consumer
    repeatedly while it's running.

    100ms period over a 1.0s window — expect ~10 ticks; assert ``>= 3``
    for headroom against CI scheduler jitter and GC pauses.
    """
    wire(Tick).to(count_ticks).from_(Source.interval(seconds=0.1))

    rt = Runtime(app_name="agent-service")
    await _run_for(rt, seconds=1.0)

    assert len(agent_counter) >= 3, (
        f"expected >=3 ticks in 1s at 100ms interval; got {len(agent_counter)}"
    )


async def test_runtime_skips_other_app_source_loops() -> None:
    """A wire whose consumer is bound to another app must NOT have its
    source loop started in this app's runtime.
    """
    wire(Tick).to(count_ticks).from_(Source.interval(seconds=0.05))
    wire(OtherTick).to(count_other_ticks).from_(Source.interval(seconds=0.05))
    bind(count_other_ticks).to_app("vectorize-worker")
    # count_ticks stays unbound -> default "agent-service".

    rt = Runtime(app_name="agent-service")
    await _run_for(rt, seconds=0.3)

    assert len(agent_counter) >= 2, (
        "agent-service wire's consumer should fire on its interval source"
    )
    assert worker_counter == [], (
        "vectorize-worker wire must not fire in the agent-service runtime; "
        f"got {len(worker_counter)} unexpected invocations"
    )


async def test_runtime_rejects_unknown_app_name() -> None:
    """An APP_NAME the placement layer doesn't know about must fail-fast.
    Otherwise ``nodes_for_app`` returns the empty set and the runtime
    boots with 0 sources / 0 consumers — looking healthy while doing
    nothing.
    """
    wire(Tick).to(count_ticks).from_(Source.interval(seconds=0.05))
    bind(count_other_ticks).to_app("vectorize-worker")
    # known_apps() now = {"agent-service", "vectorize-worker"}.
    # "totally-not-a-real-app" is not in there.

    rt = Runtime(app_name="totally-not-a-real-app")

    with pytest.raises(RuntimeError, match="totally-not-a-real-app.*known"):
        await rt.start_source_loops()


async def test_runtime_skips_its_interval_sources_in_a_test_lane(monkeypatch, caplog) -> None:
    monkeypatch.setenv("LANE", "ppe-refactor")
    monkeypatch.delenv("DATAFLOW_ENABLE_TIME_SOURCES", raising=False)
    wire(Tick).to(count_ticks).from_(Source.interval(seconds=0.05))

    rt = Runtime(app_name="agent-service")
    await _run_for(rt, seconds=0.2)

    assert agent_counter == []
    assert "skipped 1 interval source(s)" in caplog.text


class _NoTs(Data):
    """Lacks the ``ts`` field the engine builds every interval payload with."""

    tid: Annotated[str, Key]


@node
async def _never_runs(t: _NoTs) -> None:  # pragma: no cover - the payload fails first
    raise AssertionError("_never_runs should not run; the payload cannot be built")


async def test_a_payload_the_engine_cannot_build_stops_the_process(monkeypatch) -> None:
    exits: list[int] = []
    monkeypatch.setattr("os._exit", lambda code: exits.append(code))
    wire(_NoTs).to(_never_runs).from_(Source.interval(seconds=0.05))

    rt = Runtime(app_name="agent-service")
    await _run_for(rt, seconds=0.2)

    assert exits == [1]
