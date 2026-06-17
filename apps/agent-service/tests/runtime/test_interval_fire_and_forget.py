"""Task 4: interval source loop must be fire-and-forget.

赤尾范式重构核心存活地基（docs/plan/chiwei-world-objective-event-driven.md
核心范式第 4 条）：时间推进循环投出信号即进下一拍，**绝不同步等下游完成**。
任何一轮下游（world 推演）挂死 / 超时，保底心跳照常推进、世界不睡死。

两条断言对应两个正确性风险：

1. **不被堵死**：故意让一轮 emit 永远不返回（模拟 world_tick 整轮挂死），
   断言 source loop 仍按间隔投出下一拍（下一拍的 emit 照常发生），而不是被
   第一拍的挂死 emit 堵住。这是之前 coe 实测睡死的机制——旧实现 ``await
   emit(payload)`` 同步等下游，一轮卡死整个心跳停摆。

2. **异常不静默吞**：fire-and-forget 的下游抛异常时，必须被记录（log）而非
   静默丢失（``Task exception was never retrieved``），且不阻断后续拍。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Annotated

import pytest

import app.runtime.emit  # noqa: F401 — ensure the submodule is in sys.modules
from app.runtime.data import Data, Key
from app.runtime.emit import reset_emit_runtime
from app.runtime.engine import Runtime
from app.runtime.node import node
from app.runtime.placement import clear_bindings
from app.runtime.source import Source
from app.runtime.wire import clear_wiring, wire

# ``app/runtime/__init__.py`` does ``from app.runtime.emit import emit``, which
# rebinds the *package* attribute ``app.runtime.emit`` to the function object,
# shadowing the submodule. The source loop, though, does ``from app.runtime.emit
# import emit`` at call time — that resolves against the real submodule in
# ``sys.modules``. So patch the submodule object fetched from sys.modules, not
# the shadowed package attribute.
emit_mod = sys.modules["app.runtime.emit"]


class _FFTick(Data):
    ts: Annotated[str, Key]

    class Meta:
        transient = True


@node
async def _ff_consumer(_: _FFTick) -> None:  # pragma: no cover - emit is stubbed
    raise AssertionError("emit is monkeypatched in these tests; node never runs")


def setup_function() -> None:
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()


@pytest.mark.asyncio
async def test_interval_does_not_block_on_hung_downstream(monkeypatch) -> None:
    """一轮 emit 永久挂死，后续心跳仍按间隔投出（fire-and-forget）。

    旧实现 ``await emit(payload)`` 会被第一拍的挂死 emit 永久堵住——第二拍永不
    到来。fire-and-forget 后第一拍的挂死被甩进后台、源循环立刻进下一拍。
    """
    emit_starts: list[float] = []
    first_emit_released = asyncio.Event()

    async def fake_emit(payload) -> None:
        emit_starts.append(asyncio.get_event_loop().time())
        if len(emit_starts) == 1:
            # 第一拍：永久挂死（模拟 world_tick 整轮卡死 / LLM 不返回）。
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                first_emit_released.set()
                raise
        # 第二拍及之后：正常瞬时返回。

    monkeypatch.setattr(emit_mod, "emit", fake_emit)

    wire(_FFTick).from_(Source.interval(seconds=0.05)).to(_ff_consumer)

    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    await rt.start_source_loops()
    try:
        # ~6 个间隔窗口。若同步 await，第一拍挂死后再无 emit 发生（len==1）。
        await asyncio.sleep(0.35)
        assert len(emit_starts) >= 3, (
            "fire-and-forget: 第一拍挂死后心跳必须继续推进；"
            f"同步 await 会卡在第一拍。实际 emit 次数 = {len(emit_starts)}"
        )
    finally:
        await rt.stop_source_loops()


@pytest.mark.asyncio
async def test_interval_fire_and_forget_exception_is_logged_not_swallowed(
    monkeypatch, caplog
) -> None:
    """fire-and-forget 下游抛异常被记录（非静默吞），且不阻断后续拍。

    裸 ``asyncio.create_task(emit(...))`` 会让异常变成无人 retrieve 的
    ``Task exception was never retrieved``；必须有人 await / 包 try-except 记录。
    """
    emit_calls: list[int] = []

    async def fake_emit(payload) -> None:
        emit_calls.append(1)
        raise RuntimeError("downstream blew up in this tick")

    monkeypatch.setattr(emit_mod, "emit", fake_emit)

    wire(_FFTick).from_(Source.interval(seconds=0.05)).to(_ff_consumer)

    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    with caplog.at_level(logging.ERROR):
        await rt.start_source_loops()
        try:
            await asyncio.sleep(0.25)
            # 后续拍未被异常阻断：多次 emit 都发生了。
            assert len(emit_calls) >= 2, (
                "下游每拍抛异常不应阻断后续拍；"
                f"实际 emit 次数 = {len(emit_calls)}"
            )
        finally:
            await rt.stop_source_loops()

    # 异常被记录（非静默吞）。
    assert "downstream blew up in this tick" in caplog.text, (
        "fire-and-forget 下游异常必须被记录，不能静默丢失"
    )
    # 不是 fatal：watchdog / _source_error 不被触发。
    assert rt._source_error is None, (
        f"下游 emit 异常不应是 fatal；got {rt._source_error!r}"
    )
