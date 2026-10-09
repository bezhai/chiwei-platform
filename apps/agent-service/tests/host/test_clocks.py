"""Clocks: a tick every N seconds, fire-and-forget, under its own trace id; lane-gated; a tick
that cannot even build its work stops the process.

Ported from the dataflow engine's interval-source tests (``test_engine_phase4``,
``test_engine_source_error``, ``test_interval_fire_and_forget``): the loop they pinned is
:mod:`app.host.clock`, which the host runs for ``ctx.clock``.

Why fire-and-forget (from the original): a round that hangs or times out must not stop the next
tick. That is how world once slept for good on coe: the loop awaited a round stuck on an LLM call.
So every tick's work runs in its own task, its errors are logged (never "Task exception was never
retrieved"), and stop cancels the ones still running.
"""
from __future__ import annotations

import asyncio
import logging
import re

import pytest

from app.api.middleware import lane_var, trace_id_var
from app.host import Host

from .conftest import plugin


@pytest.fixture(autouse=True)
def _prod_lane(monkeypatch):
    monkeypatch.delenv("LANE", raising=False)
    monkeypatch.delenv("DATAFLOW_ENABLE_TIME_SOURCES", raising=False)


@pytest.fixture
def exits(monkeypatch) -> list[int]:
    codes: list[int] = []
    monkeypatch.setattr("os._exit", lambda code: codes.append(code))
    return codes


def _clock_host(tick, *, seconds: float = 0.05, name: str = "c") -> Host:
    return Host("agent-service", [plugin("p", lambda ctx: ctx.clock(name, seconds, tick))])


async def _start(host: Host) -> None:
    await host.start(http=None, schema=False, mq=False, clocks=True, tasks=False)


def _clock_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("clock[")]


async def test_a_clock_ticks_every_interval():
    seen: list[object] = []

    async def note(ts) -> None:
        seen.append(ts)

    host = _clock_host(note)
    await _start(host)
    try:
        await asyncio.sleep(0.2)
        assert len(seen) >= 2
    finally:
        await host.stop()


async def test_a_hung_tick_does_not_hold_up_the_next():
    """One tick's work never returns; the clock keeps ticking anyway."""
    starts: list[float] = []

    async def first_one_hangs(ts) -> None:
        starts.append(asyncio.get_running_loop().time())
        if len(starts) == 1:
            await asyncio.Event().wait()

    host = _clock_host(first_one_hangs)
    await _start(host)
    try:
        await asyncio.sleep(0.35)
        assert len(starts) >= 3, f"stuck behind the first tick: {len(starts)} tick(s)"
    finally:
        await host.stop()


async def test_a_failing_tick_is_logged_and_the_clock_keeps_going(caplog, exits):
    calls: list[int] = []

    async def blows_up(ts) -> None:
        calls.append(1)
        raise RuntimeError("downstream blew up in this tick")

    host = _clock_host(blows_up)
    with caplog.at_level(logging.ERROR):
        await _start(host)
        try:
            await asyncio.sleep(0.25)
            assert len(calls) >= 2, "one failing tick stopped the clock"
        finally:
            await host.stop()

    assert "downstream blew up in this tick" in caplog.text, "a tick's error was swallowed"
    assert exits == [], "a failing tick is not fatal"


async def test_a_tick_that_cannot_build_its_work_stops_the_process(exits):
    """``tick(ts)`` runs inside the loop, so an error building the work (a payload missing a
    field, say) reaches the watchdog, which exits the process for PaaS to restart it."""

    def cannot_build(ts):
        raise TypeError("LifeMomentTick requires a 'ts: str' field")

    host = _clock_host(cannot_build)
    await _start(host)
    await asyncio.sleep(0.2)
    await host.stop()

    assert exits == [1]


async def test_a_normal_stop_does_not_exit(exits):
    async def note(ts) -> None:
        return None

    host = _clock_host(note)
    await _start(host)
    await asyncio.sleep(0.1)
    await host.stop()

    assert exits == []


async def test_stop_cancels_the_ticks_still_running_and_no_tick_fires_after():
    """T1 acceptance (the clock half): after stop the clock's tasks are done and nothing ticks."""
    started: list[int] = []
    cancelled: list[int] = []

    async def hangs(ts) -> None:
        started.append(1)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    host = _clock_host(hangs)
    await _start(host)
    await asyncio.sleep(0.12)
    loops = _clock_tasks()
    assert loops, "the clock loop should be a task named clock[...]"

    await host.stop()
    fired = len(started)
    await asyncio.sleep(0.2)

    assert all(t.done() for t in loops)
    assert _clock_tasks() == []
    assert fired >= 1 and len(cancelled) == fired, "a tick still running was left behind"
    assert len(started) == fired, "a tick fired after stop"


async def test_each_tick_runs_under_its_own_interval_trace_id():
    """A clock has no inbound trace; each tick gets ``interval:<seconds>s:<8 hex>`` and no lane,
    so the rounds it starts stay linked in Langfuse."""
    seen: list[tuple[str | None, str | None]] = []

    async def note(ts) -> None:
        seen.append((trace_id_var.get(), lane_var.get()))

    host = _clock_host(note)
    await _start(host)
    try:
        await asyncio.sleep(0.2)
    finally:
        await host.stop()

    assert len(seen) >= 2
    for trace_id, lane in seen:
        assert re.fullmatch(r"interval:0\.05s:[0-9a-f]{8}", trace_id), trace_id
        assert lane is None
    assert len({trace_id for trace_id, _ in seen}) == len(seen)


async def test_whole_seconds_stay_a_float_in_the_trace_id():
    """``interval:60.0s:…`` is what the five living clocks write today; ``60`` must not turn it
    into ``interval:60s:…``."""
    seen: list[str | None] = []

    async def note(ts) -> None:
        seen.append(trace_id_var.get())

    host = _clock_host(note, seconds=1)
    await _start(host)
    try:
        await asyncio.sleep(1.15)
    finally:
        await host.stop()

    assert seen and seen[0].startswith("interval:1.0s:")


async def test_clocks_do_not_run_in_a_test_lane_by_default(monkeypatch, caplog):
    monkeypatch.setenv("LANE", "ppe-refactor")
    seen: list[object] = []

    async def note(ts) -> None:
        seen.append(ts)

    host = _clock_host(note)
    await _start(host)
    try:
        await asyncio.sleep(0.2)
    finally:
        await host.stop()

    assert seen == []
    assert "skipped 1 interval source(s) in lane=ppe-refactor" in caplog.text
    assert "DATAFLOW_ENABLE_TIME_SOURCES=1" in caplog.text


async def test_the_override_runs_clocks_in_a_test_lane(monkeypatch):
    monkeypatch.setenv("LANE", "ppe-refactor")
    monkeypatch.setenv("DATAFLOW_ENABLE_TIME_SOURCES", "1")
    seen: list[object] = []

    async def note(ts) -> None:
        seen.append(ts)

    host = _clock_host(note)
    await _start(host)
    try:
        await asyncio.sleep(0.2)
    finally:
        await host.stop()

    assert len(seen) >= 1


async def test_tasks_are_not_lane_gated(monkeypatch):
    """Only clocks are: the skill reload loop runs in every lane today."""
    monkeypatch.setenv("LANE", "ppe-refactor")
    ran = asyncio.Event()

    async def run() -> None:
        ran.set()
        await asyncio.Event().wait()

    host = Host("agent-service", [plugin("p", lambda ctx: ctx.task("reload", run))])
    await host.start(http=None, schema=False, mq=False, clocks=True, tasks=True)
    try:
        await asyncio.wait_for(ran.wait(), timeout=1)
    finally:
        await host.stop()


async def test_a_clock_disposer_stops_that_clock_and_no_other():
    disposers: dict[str, object] = {}
    seen: dict[str, int] = {"gone": 0, "kept": 0}

    def counting(name):
        async def note(ts) -> None:
            seen[name] += 1

        return note

    def setup(ctx) -> None:
        disposers["gone"] = ctx.clock("gone", 0.05, counting("gone"))
        ctx.clock("kept", 0.05, counting("kept"))

    host = Host("agent-service", [plugin("p", setup)])
    await _start(host)
    try:
        await asyncio.sleep(0.12)
        await disposers["gone"]()
        gone_then, kept_then = seen["gone"], seen["kept"]
        await asyncio.sleep(0.2)

        assert seen["gone"] == gone_then
        assert seen["kept"] > kept_then
        assert [t.get_name() for t in _clock_tasks()] == ["clock[kept]"]
    finally:
        await host.stop()
