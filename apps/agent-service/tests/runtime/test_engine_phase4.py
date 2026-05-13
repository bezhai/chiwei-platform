"""Phase 4 runtime extensions: Source.cron tz + start_source_loops + watchdog."""
from __future__ import annotations

import asyncio
from typing import Annotated
from zoneinfo import ZoneInfo

import pytest

from app.runtime.data import Data, Key
from app.runtime.emit import reset_emit_runtime
from app.runtime.engine import Runtime
from app.runtime.node import node
from app.runtime.placement import clear_bindings
from app.runtime.source import Source, SourceSpec
from app.runtime.wire import clear_wiring, wire


def test_source_cron_default_tz_is_utc():
    spec = Source.cron("* * * * *")
    assert isinstance(spec, SourceSpec)
    assert spec.kind == "cron"
    assert spec.params["expr"] == "* * * * *"
    assert spec.params["tz"] == "UTC"


def test_source_cron_accepts_tz():
    spec = Source.cron("0 5 * * *", tz="Asia/Shanghai")
    assert spec.params["tz"] == "Asia/Shanghai"


class _TzTick(Data):
    ts: Annotated[str, Key]


_tz_emitted: list[_TzTick] = []


@node
async def _record_tz_tick(t: _TzTick) -> None:
    _tz_emitted.append(t)


@pytest.mark.asyncio
async def test_cron_source_uses_declared_tz(monkeypatch):
    """croniter base must be in the declared tz so cron expressions are
    interpreted at the right wall clock."""
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    _tz_emitted.clear()

    captured: dict = {}

    def fake_croniter(expr, base):
        captured["base"] = base
        captured["expr"] = expr
        # Stub croniter that returns a single far-future tick so the loop
        # awaits and the test cancels it.
        class _Iter:
            def get_next(self, _t):
                return base.replace(year=base.year + 1)
        return _Iter()

    monkeypatch.setattr("croniter.croniter", fake_croniter)

    wire(_TzTick).from_(Source.cron("0 5 * * *", tz="Asia/Shanghai")).to(_record_tz_tick)

    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    # Use start_source_loops once Task 2 lands; for now invoke private
    # helper to validate tz wiring. Rewrite this test in Task 2.
    # NOTE: this assertion requires _source_loop_cron to honor tz.
    task = asyncio.create_task(rt._source_loop_cron(
        next(w for w in __import__("app.runtime.graph", fromlist=["compile_graph"]).compile_graph().wires if w.data_type is _TzTick),
        Source.cron("0 5 * * *", tz="Asia/Shanghai"),
    ))
    await asyncio.sleep(0.05)
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass

    assert captured["base"].tzinfo == ZoneInfo("Asia/Shanghai")


class _StartTick(Data):
    ts: Annotated[str, Key]


_start_seen: list[_StartTick] = []


@node
async def _record_start(t: _StartTick) -> None:
    _start_seen.append(t)


@pytest.mark.asyncio
async def test_start_source_loops_starts_only_sources():
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    _start_seen.clear()

    wire(_StartTick).from_(Source.interval(seconds=0.05)).to(_record_start)

    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    await rt.start_source_loops()
    try:
        await asyncio.sleep(0.2)
        assert len(_start_seen) >= 2
    finally:
        await rt.stop_source_loops()


@pytest.mark.asyncio
async def test_normal_stop_does_not_exit(monkeypatch):
    """stop_source_loops on the happy path must not call os._exit."""
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    _start_seen.clear()

    exits: list[int] = []
    monkeypatch.setattr("os._exit", lambda code: exits.append(code))

    wire(_StartTick).from_(Source.interval(seconds=0.05)).to(_record_start)
    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    await rt.start_source_loops()
    await asyncio.sleep(0.1)
    await rt.stop_source_loops()

    assert exits == []


class _BadTick(Data):
    """Lacks required 'ts' field — _build_payload (non-emit path) raises."""

    tid: Annotated[str, Key]


@node
async def _bad_consumer(t: _BadTick) -> None:  # pragma: no cover — never reached
    raise AssertionError("_bad_consumer should not run; build_payload errors first")


@pytest.mark.asyncio
async def test_watchdog_exits_on_source_error(monkeypatch):
    """A fatal source loop error (non-emit path) triggers os._exit(1).

    Contract §4.1 (A2): only **infra / payload-build / clock setup** failures
    along the source-loop are fatal; emit() exceptions are log+continue.
    This test uses a Data without a ``ts`` field so ``_build_payload``
    raises BEFORE emit() — confirming the still-fatal classification.
    The "consumer raises on every tick" case is owned by
    ``tests/runtime/test_engine_source_error.py``.
    """
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()

    exits: list[int] = []
    monkeypatch.setattr("os._exit", lambda code: exits.append(code))

    wire(_BadTick).from_(Source.interval(seconds=0.05)).to(_bad_consumer)

    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    await rt.start_source_loops()
    await asyncio.sleep(0.2)  # give watchdog time to react
    await rt.stop_source_loops()

    assert exits == [1]
