"""A2: cron/interval source error classification (contract §4.1).

Behavior contract under the new error分级:

* ``emit()`` 内任何业务/wire 异常（包括 in-process consumer raise） →
  log warning + 跳过本 tick + 继续下一个 tick，**不杀 pod**.
* 非 emit 路径（payload build / croniter init / 时钟 setup 等）抛
  ``Exception`` → 仍走 ``_record_source_error`` → watchdog kill pod
  （PaaS 重启）.

旧契约一律把任何 source-loop 异常 ``_record_source_error`` → 杀 pod，
本测试是新契约 (A2) 落地后才成立的；与之冲突的 legacy 用例
(``test_watchdog_exits_on_source_error``) 已在同次改造内迁移到此文件
明确两条分支的边界。
"""

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


class _IntervalTick(Data):
    ts: Annotated[str, Key]


class _CronTick(Data):
    ts: Annotated[str, Key]


_raise_call_count = {"n": 0}


@node
async def _always_raises_interval(t: _IntervalTick) -> None:
    _raise_call_count["n"] += 1
    raise RuntimeError("consumer fails on every tick")


@node
async def _always_raises_cron(t: _CronTick) -> None:
    _raise_call_count["n"] += 1
    raise RuntimeError("cron consumer fails on every tick")


def setup_function() -> None:
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    _raise_call_count["n"] = 0


@pytest.mark.asyncio
async def test_interval_emit_exception_does_not_kill_pod(monkeypatch) -> None:
    """contract §4.1: interval source 内 emit() 抛 Exception →
    log + continue ticking, 不触发 _record_source_error / watchdog os._exit.
    """
    exits: list[int] = []
    monkeypatch.setattr("os._exit", lambda code: exits.append(code))

    wire(_IntervalTick).from_(Source.interval(seconds=0.05)).to(
        _always_raises_interval
    )

    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    await rt.start_source_loops()
    # Long enough for several ticks to fire and raise.
    await asyncio.sleep(0.25)
    # _source_error must stay None — loop kept ticking through failures.
    assert rt._source_error is None, (
        f"interval emit exception must not be fatal; got {rt._source_error!r}"
    )
    # Consumer fired at least twice (proves loop survived first failure).
    assert _raise_call_count["n"] >= 2, (
        f"expected loop to keep ticking past first failure; "
        f"got {_raise_call_count['n']} consumer invocations"
    )
    await rt.stop_source_loops()
    # watchdog must NOT have called os._exit.
    assert exits == [], f"unexpected os._exit call(s): {exits}"


@pytest.mark.asyncio
async def test_cron_emit_exception_does_not_kill_pod(monkeypatch) -> None:
    """contract §4.1: cron source 内 emit() 抛 Exception →
    log + continue ticking, 不触发 _record_source_error / watchdog os._exit.

    croniter is stubbed to fire every ~50 ms so the test runs fast.
    """
    exits: list[int] = []
    monkeypatch.setattr("os._exit", lambda code: exits.append(code))

    from datetime import datetime, timedelta

    def fake_croniter(expr, base):
        class _Iter:
            def __init__(self):
                self._cur = base

            def get_next(self, _t):
                self._cur = self._cur + timedelta(milliseconds=50)
                return self._cur

        return _Iter()

    monkeypatch.setattr("croniter.croniter", fake_croniter)

    wire(_CronTick).from_(Source.cron("* * * * *")).to(_always_raises_cron)

    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    await rt.start_source_loops()
    await asyncio.sleep(0.3)
    assert rt._source_error is None, (
        f"cron emit exception must not be fatal; got {rt._source_error!r}"
    )
    assert _raise_call_count["n"] >= 2, (
        f"expected cron loop to keep ticking past failure; "
        f"got {_raise_call_count['n']} consumer invocations"
    )
    await rt.stop_source_loops()
    assert exits == [], f"unexpected os._exit call(s): {exits}"


class _NoTsField(Data):
    """Lacks a 'ts' field — _build_payload will raise outside emit()."""

    tid: Annotated[str, Key]


@node
async def _unreachable(_: _NoTsField) -> None:  # pragma: no cover
    raise AssertionError("must never run")


@pytest.mark.asyncio
async def test_payload_build_failure_is_still_fatal(monkeypatch) -> None:
    """contract §4.1: source-loop 非 emit 路径异常（payload build /
    croniter 初始化等）仍然触发 _record_source_error → watchdog kill pod.
    """
    exits: list[int] = []
    monkeypatch.setattr("os._exit", lambda code: exits.append(code))

    wire(_NoTsField).from_(Source.interval(seconds=0.05)).to(_unreachable)

    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    await rt.start_source_loops()
    # _build_payload raises immediately on the first tick → fatal.
    await asyncio.sleep(0.2)
    assert rt._source_error is not None, (
        "payload build failure must be fatal (non-emit path)"
    )
    assert isinstance(rt._source_error, RuntimeError)
    await rt.stop_source_loops()
    # watchdog should have been triggered → os._exit(1).
    assert exits == [1], f"expected watchdog kill pod; got exits={exits}"
