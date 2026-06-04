"""world/life event 闭环的 wiring — stage3 联调收口.

把 stage1/2/3 各自单测绿了的零件拼成一个能动的世界，靠这四条新 wire：

  1. ``Source.interval(600) -> WorldHeartbeatTick -> heartbeat_to_world_tick``：
     保底心跳（≤10 分钟）。时间源喂单字段 WorldHeartbeatTick（满足框架单字段 ts
     约定），翻译节点补 lane/reason emit WorldTick；WorldTick 经一条纯 in-process
     边接回 world_tick，world_tick 内部 ``emit_delayed(WorldTick(reason="self"))``
     的自排回环也走这同一条 in-process 边（三源同一入口）。
  2. ``IntentRaised -> intent_to_world_tick -> WorldTick -> world_tick``：life
     回灌意图翻成 WorldTick(reason="intent")，durable 跨进程。
  3. ``EventArrived.debounce(key_by=event_knock_key) -> life_wake_node``：攒批
     唤醒 life（多条积压只醒一次）。

stage4 才删旧 wire；本测试只验"新 wire 加上了、graph 仍能编译"，不验旧 wire
被删（新旧并存）。
"""

from __future__ import annotations

import importlib


def _fresh_import():
    """重新加载 wiring，让 wire(...) 语句重跑、WIRING_REGISTRY 从头填。"""
    import app.wiring.life_dataflow as ld
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(ld)


def _wires_for(name: str):
    from app.runtime.wire import WIRING_REGISTRY

    return [w for w in WIRING_REGISTRY if w.data_type.__name__ == name]


def test_graph_still_compiles_with_world_life_wires():
    """加上 world/life event 的四条新 wire 后，整图仍能编译（无 GraphError）。"""
    _fresh_import()
    from app.runtime.graph import compile_graph

    graph = compile_graph()
    assert graph is not None


def test_heartbeat_interval_drives_world_tick():
    """保底心跳：Source.interval(600) → WorldHeartbeatTick → heartbeat_to_world_tick。

    时间源不直接喂 WorldTick（那会在源循环 _build_payload(WorldTick(ts=...)) 处
    ValidationError 杀 Pod —— WorldTick 无 ts、缺必填 lane）。interval 源喂单字段
    WorldHeartbeatTick（满足框架单字段 ts 约定），翻译节点 heartbeat_to_world_tick
    再补 lane/reason emit WorldTick 接回 world_tick（in-process 边由
    test_self_loop_world_tick_routes_back_in_process 覆盖）。
    """
    _fresh_import()
    from app.world.engine import WORLD_HEARTBEAT_SECONDS, heartbeat_to_world_tick

    heartbeat_wires = _wires_for("WorldHeartbeatTick")
    # WorldHeartbeatTick 至少有一条 wire 接到翻译节点 heartbeat_to_world_tick
    consumer_wires = [
        w for w in heartbeat_wires if heartbeat_to_world_tick in w.consumers
    ]
    assert consumer_wires, "WorldHeartbeatTick 没有接到 heartbeat_to_world_tick"

    # 这条 wire 带 interval 心跳源，且周期 == 10 分钟保底心跳
    interval_wires = [
        w
        for w in consumer_wires
        for s in w.sources
        if s.kind == "interval"
    ]
    assert interval_wires, "缺少 interval 心跳源驱动 heartbeat_to_world_tick"
    src = next(s for w in consumer_wires for s in w.sources if s.kind == "interval")
    assert src.params["seconds"] == float(WORLD_HEARTBEAT_SECONDS)


def test_self_loop_world_tick_routes_back_in_process():
    """自排回环：world_tick emit 的 WorldTick 经 in-process 边接回 world_tick。

    world_tick 内部 emit_delayed(WorldTick(reason="self")) 到期后 emit(WorldTick)，
    必须有一条 in-process（非 durable）的 wire 把它接回 world_tick，否则自排空转。
    心跳源那条 wire 同时承载自排回环（三源同一入口）。
    """
    _fresh_import()
    from app.world.engine import world_tick

    consumer_wires = [w for w in _wires_for("WorldTick") if world_tick in w.consumers]
    # 至少有一条接 world_tick 的 WorldTick wire 是 in-process（自排 / 心跳 emit 走它）
    assert any(not w.durable for w in consumer_wires), (
        "WorldTick → world_tick 没有 in-process 边，自排回环 / 心跳 emit 会空转"
    )


def test_intent_raised_translated_durably_to_world():
    """life 回灌意图：IntentRaised → intent_to_world_tick，durable 跨进程。"""
    _fresh_import()
    from app.world.engine import intent_to_world_tick

    intent_wires = _wires_for("IntentRaised")
    translated = [w for w in intent_wires if intent_to_world_tick in w.consumers]
    assert translated, "IntentRaised 没有接到 intent_to_world_tick 翻译节点"
    # durable：life 进程 → world 进程跨进程可达且不丢
    assert any(w.durable for w in translated), (
        "IntentRaised → intent_to_world_tick 必须 durable（跨进程回灌）"
    )


def test_event_arrived_debounced_to_life_wake():
    """攒批唤醒 life：EventArrived.debounce(key_by=event_knock_key) → life_wake_node。"""
    _fresh_import()
    from app.domain.world_events import event_knock_key
    from app.nodes.life_wake import life_wake_node

    arrived_wires = _wires_for("EventArrived")
    debounced = [
        w
        for w in arrived_wires
        if w.debounce is not None and life_wake_node in w.consumers
    ]
    assert debounced, "EventArrived 没有 debounce 攒批接到 life_wake_node"
    w = debounced[0]
    # 攒批分区键复用 event_knock_key（与信箱隔离口径一致）
    assert w.debounce_key_by is event_knock_key
    # debounce 参数合理（窗口 > 0、max_buffer > 0）
    assert w.debounce["seconds"] > 0
    assert w.debounce["max_buffer"] > 0
