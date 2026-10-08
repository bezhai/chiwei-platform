"""Phase 4 runtime extensions: start_source_loops + watchdog."""
from __future__ import annotations

import asyncio
from typing import Annotated

import pytest

from app.runtime.data import Data, Key
from app.runtime.emit import reset_emit_runtime
from app.runtime.engine import Runtime
from app.runtime.node import node
from app.runtime.placement import clear_bindings
from app.runtime.source import Source
from app.runtime.wire import clear_wiring, wire


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

    rt = Runtime(app_name="agent-service")
    await rt.start_source_loops()
    try:
        await asyncio.sleep(0.2)
        assert len(_start_seen) >= 2
    finally:
        await rt.stop_source_loops()


@pytest.mark.asyncio
async def test_start_source_loops_skips_time_sources_in_ppe(monkeypatch, caplog):
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    _start_seen.clear()
    monkeypatch.setenv("LANE", "ppe-refactor")
    monkeypatch.delenv("DATAFLOW_ENABLE_TIME_SOURCES", raising=False)

    wire(_StartTick).from_(Source.interval(seconds=0.05)).to(_record_start)

    rt = Runtime(app_name="agent-service")
    await rt.start_source_loops()
    try:
        await asyncio.sleep(0.2)
        assert _start_seen == []
        assert "skipped 1 interval source(s)" in caplog.text
    finally:
        await rt.stop_source_loops()


@pytest.mark.asyncio
async def test_start_source_loops_override_allows_time_sources_in_ppe(monkeypatch):
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    _start_seen.clear()
    monkeypatch.setenv("LANE", "ppe-refactor")
    monkeypatch.setenv("DATAFLOW_ENABLE_TIME_SOURCES", "1")

    wire(_StartTick).from_(Source.interval(seconds=0.05)).to(_record_start)

    rt = Runtime(app_name="agent-service")
    await rt.start_source_loops()
    try:
        await asyncio.sleep(0.2)
        assert len(_start_seen) >= 1
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
    rt = Runtime(app_name="agent-service")
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

    rt = Runtime(app_name="agent-service")
    await rt.start_source_loops()
    await asyncio.sleep(0.2)  # give watchdog time to react
    await rt.stop_source_loops()

    assert exits == [1]
