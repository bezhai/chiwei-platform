"""Phase 4 runtime extensions: Source.cron tz + start_source_loops + watchdog."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated
from unittest.mock import patch
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
