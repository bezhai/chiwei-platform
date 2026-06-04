"""意图回灌 world 这条边 — Task 1.

life "我想做什么" → emit ``IntentRaised`` → 唤醒 world 去裁决。world 节点本身
是 Task 2 的活；Task 1 立的是 ``IntentRaised`` 的数据形态 + ``raise_intent``
emit helper + "意图能回灌唤醒 world"这条边。world 端用测试 stub node 验证收到。

意图走 ``.durable()``（跨进程 life → world 可达且不丢）。这里证明 emit 走
durable publish 通道（不 in-process 直调），等价于"意图确实被投去唤醒 world"。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.domain.world_events as we_mod
from app.domain.world_events import IntentRaised, raise_intent
from app.runtime.emit import emit, reset_emit_runtime
from app.runtime.graph import compile_graph
from app.runtime.node import _NODE_META, NODE_REGISTRY, node
from app.runtime.placement import clear_bindings
from app.runtime.wire import clear_wiring, wire


@pytest.fixture(autouse=True)
def _isolation():
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


def test_intent_is_durable_not_transient():
    """意图跨进程回灌 world,必须可持久化(非 transient) —— durable 边硬约束。"""
    meta = getattr(IntentRaised, "Meta", None)
    assert not (meta and getattr(meta, "transient", False))


def test_intent_carries_what_world_needs():
    """意图的数据形态带齐 world 裁决所需：谁起的、起了啥、何时、哪个泳道。"""
    fields = set(IntentRaised.model_fields)
    assert {"lane", "intent_id", "persona_id", "summary", "occurred_at"} <= fields


@pytest.mark.asyncio
async def test_raise_intent_emits_intent(monkeypatch):
    """raise_intent(...) → emit 一条 IntentRaised,字段照传。"""
    fake_emit = AsyncMock()
    monkeypatch.setattr(we_mod, "emit", fake_emit)

    await raise_intent(
        lane="coe-t1",
        intent_id="i1",
        persona_id="akao",
        summary="我想去厨房煮咖啡",
        occurred_at="2026-06-03T08:00:00Z",
    )

    fake_emit.assert_awaited_once()
    intent = fake_emit.await_args.args[0]
    assert isinstance(intent, IntentRaised)
    assert intent.lane == "coe-t1"
    assert intent.intent_id == "i1"
    assert intent.persona_id == "akao"
    assert intent.summary == "我想去厨房煮咖啡"


@pytest.mark.asyncio
async def test_intent_edge_wakes_world_via_durable(monkeypatch):
    """意图回灌 world 的边：emit(IntentRaised) 走 durable publish 唤醒 world。

    world 节点是 Task 2 的；这里用 stub 验证"边接上了、收到了"。emit 必须走
    durable 通道（不 in-process 直调），证明意图被投去唤醒另一进程的 world。
    """
    received: list = []

    @node
    async def _world_stub(_i: IntentRaised) -> None:
        received.append(_i)

    wire(IntentRaised).to(_world_stub).durable()

    g = compile_graph()  # durable + 非 transient + 单 consumer：形态合法
    assert IntentRaised in g.data_types

    fake_publish = AsyncMock()
    monkeypatch.setattr("app.runtime.durable.publish_durable", fake_publish)

    intent = IntentRaised(
        lane="coe-t1", intent_id="i1", persona_id="akao",
        summary="我想去厨房", occurred_at="2026-06-03T08:00:00Z",
    )
    await emit(intent)

    fake_publish.assert_awaited_once()
    # publish_durable(w, consumer, data)
    assert fake_publish.await_args.args[1] is _world_stub
    assert fake_publish.await_args.args[2] is intent
    assert received == []  # 没被 in-process 直调,确实走了跨进程唤醒
