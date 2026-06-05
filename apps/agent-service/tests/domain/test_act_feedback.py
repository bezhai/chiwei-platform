"""动作回灌 world 这条边 — 阶段 1A 契约基石.

新范式：角色用 ``act`` 自主做事（自然语言），world 只推演这件事的客观结果、
不批准。Data 层的体现是 ``ActPerformed``（她做了的事），替掉旧的
``IntentRaised``（意图待裁决）。

life 自主做了一件影响外部世界的事 → emit ``ActPerformed`` → durable 回灌
唤醒 world 去**推演客观结果**。world 节点本身是后续子 agent 的活；这里立的
是 ``ActPerformed`` 的数据形态 + ``perform_act`` emit helper + "动作能回灌
唤醒 world"这条边。world 端用测试 stub node 验证收到。

动作走 ``.durable()``（跨进程 life → world 可达且不丢）。这里证明 emit 走
durable publish 通道（不 in-process 直调），等价于"动作确实被投去唤醒 world"。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.domain.world_events as we_mod
from app.domain.world_events import ActPerformed, perform_act
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


def test_act_is_durable_not_transient():
    """动作跨进程回灌 world,必须可持久化(非 transient) —— durable 边硬约束。"""
    meta = getattr(ActPerformed, "Meta", None)
    assert not (meta and getattr(meta, "transient", False))


def test_act_carries_what_world_needs():
    """动作的数据形态带齐 world 推演所需：谁做的、做了啥、何时、哪个泳道。"""
    fields = set(ActPerformed.model_fields)
    assert {"lane", "act_id", "persona_id", "description", "occurred_at"} <= fields


def test_act_natural_key_is_lane_and_act_id():
    """自然键是 (lane, act_id)：world 端按 act_id 幂等消化，lane 泳道隔离硬约束。"""
    from app.runtime.data import key_fields

    assert key_fields(ActPerformed) == ("lane", "act_id")


def test_act_fields_are_scalar_for_framework_persistence():
    """所有字段都是标量 str —— framework 不能序列化 dict/list 进 JSONB，
    durable 持久化（emit → insert_idempotent/insert_append）只吃 TEXT/标量。"""
    for name, fi in ActPerformed.model_fields.items():
        assert fi.annotation is str, f"{name} 必须是 str,实际 {fi.annotation!r}"


@pytest.mark.asyncio
async def test_perform_act_emits_act(monkeypatch):
    """perform_act(...) → emit 一条 ActPerformed,字段照传。"""
    fake_emit = AsyncMock()
    monkeypatch.setattr(we_mod, "emit", fake_emit)

    await perform_act(
        lane="coe-t1",
        act_id="a1",
        persona_id="akao",
        description="我去厨房做饭",
        occurred_at="2026-06-05T08:00:00Z",
    )

    fake_emit.assert_awaited_once()
    act = fake_emit.await_args.args[0]
    assert isinstance(act, ActPerformed)
    assert act.lane == "coe-t1"
    assert act.act_id == "a1"
    assert act.persona_id == "akao"
    assert act.description == "我去厨房做饭"
    assert act.occurred_at == "2026-06-05T08:00:00Z"


@pytest.mark.asyncio
async def test_act_edge_wakes_world_via_durable(monkeypatch):
    """动作回灌 world 的边：emit(ActPerformed) 走 durable publish 唤醒 world。

    world 节点是后续子 agent 的；这里用 stub 验证"边接上了、收到了"。emit 必须走
    durable 通道（不 in-process 直调），证明动作被投去唤醒另一进程的 world。
    """
    received: list = []

    @node
    async def _world_stub(_a: ActPerformed) -> None:
        received.append(_a)

    wire(ActPerformed).to(_world_stub).durable()

    g = compile_graph()  # durable + 非 transient + 单 consumer：形态合法
    assert ActPerformed in g.data_types

    fake_publish = AsyncMock()
    monkeypatch.setattr("app.runtime.durable.publish_durable", fake_publish)

    act = ActPerformed(
        lane="coe-t1", act_id="a1", persona_id="akao",
        description="我去厨房做饭", occurred_at="2026-06-05T08:00:00Z",
    )
    await emit(act)

    fake_publish.assert_awaited_once()
    # publish_durable(w, consumer, data)
    assert fake_publish.await_args.args[1] is _world_stub
    assert fake_publish.await_args.args[2] is act
    assert received == []  # 没被 in-process 直调,确实走了跨进程唤醒
