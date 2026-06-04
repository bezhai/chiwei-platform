"""敲门信号 + debounce 攒批唤醒 — Task 1 (event 流转骨架).

信箱来新 event 不是来一条醒一次，而是攒批一次唤醒 life。机制：投递成功后
emit 一个 transient ``EventArrived`` 敲门信号；该信号走 framework 的 debounce
原语攒批，多条积压只唤醒 life 一次。内容不在信号里（信号只带 lane+persona），
在 durable 信箱里。

Task 1 立的是「敲门信号的 Data 形态 + 攒批 wire 形态 + 投递→敲门这条边」。
debounce wire 的 consumer 是 life-wake 节点，那是 Task 3 的活，这里用测试 stub
node 验证 wire 形态合法 + emit 走攒批路径（不 in-process 直调），不碰 life 节点。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.domain.world_events import EventArrived, event_knock_key
from app.runtime.emit import emit, reset_emit_runtime
from app.runtime.graph import compile_graph
from app.runtime.node import _NODE_META, NODE_REGISTRY, node
from app.runtime.placement import clear_bindings
from app.runtime.wire import clear_wiring, wire


@pytest.fixture(autouse=True)
def _isolation():
    """Snapshot/restore registries so inline @node consumers don't leak."""
    nodes_snap = set(NODE_REGISTRY)
    meta_snap = dict(_NODE_META)
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    try:
        yield
    finally:
        NODE_REGISTRY.clear()
        NODE_REGISTRY.update(nodes_snap)
        _NODE_META.clear()
        _NODE_META.update(meta_snap)
        clear_wiring()
        clear_bindings()
        reset_emit_runtime()


def test_event_arrived_is_transient():
    """敲门信号必须 transient —— debounce 硬约束（不落 pg）。"""
    assert getattr(EventArrived.Meta, "transient", False) is True


def test_knock_key_partitions_by_lane_and_persona():
    """攒批分区键按 (lane, persona) 区分：不同 persona / 不同 lane 各自攒批。"""
    a = EventArrived(lane="coe-t1", persona_id="akao")
    b = EventArrived(lane="coe-t1", persona_id="chinagi")
    c = EventArrived(lane="prod", persona_id="akao")
    assert event_knock_key(a) != event_knock_key(b)
    assert event_knock_key(a) != event_knock_key(c)
    assert event_knock_key(a) == event_knock_key(
        EventArrived(lane="coe-t1", persona_id="akao")
    )


def test_debounce_wire_shape_compiles():
    """EventArrived 的 debounce 攒批 wire 形态能通过 compile_graph 校验。

    证明这条 wire 形态合法（transient + 单 consumer + key_by），Task 3 可以
    直接把 life-wake 节点接到这个 wire 上。
    """

    @node
    async def _wake_stub(_a: EventArrived) -> None: ...

    wire(EventArrived).debounce(
        seconds=30, max_buffer=20, key_by=event_knock_key
    ).to(_wake_stub)

    g = compile_graph()  # 不抛 = 形态合法
    assert EventArrived in g.data_types


@pytest.mark.asyncio
async def test_emit_knock_goes_through_debounce_not_inprocess(monkeypatch):
    """emit(EventArrived) 在 debounce wire 上必须走攒批路径（publish_debounce），
    不能 in-process 直接唤醒 —— 这就是"攒批一次唤醒、不来一条醒一次"的机制保证。
    """
    woke: list = []

    @node
    async def _wake_stub(_a: EventArrived) -> None:
        woke.append(_a)

    wire(EventArrived).debounce(
        seconds=30, max_buffer=20, key_by=event_knock_key
    ).to(_wake_stub)

    fake_publish = AsyncMock()
    monkeypatch.setattr("app.runtime.debounce.publish_debounce", fake_publish)

    # 模拟一批积压：三条到达，都应只走 publish_debounce、不直接唤醒
    for _ in range(3):
        await emit(EventArrived(lane="coe-t1", persona_id="akao"))

    assert fake_publish.await_count == 3  # 每条都进攒批通道
    assert woke == []  # life-wake 节点没被 in-process 直调
    # 攒批的真实"3 条 → 1 次 fire"由 framework debounce redis CAS 保证
    # （tests/runtime/test_debounce.py 已覆盖），这里证明的是我们的 wire
    # 确实走了攒批通道。
