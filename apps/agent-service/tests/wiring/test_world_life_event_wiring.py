"""world/life event 闭环的 wiring — pull 范式联调收口.

把各自单测绿了的零件拼成一个能动的世界，靠这几条 wire：

  1. ``Source.interval(600) -> WorldHeartbeatTick -> heartbeat_to_world_tick``：
     保底心跳（≤10 分钟）。时间源喂单字段 WorldHeartbeatTick（满足框架单字段 ts
     约定），翻译节点补 lane/reason emit WorldTick；WorldTick 经一条纯 in-process
     边接回 world_tick，world_tick 内部 ``emit_delayed(WorldTick(reason="self"))``
     的自排回环也走这同一条 in-process 边（两源同一入口）。
  2. **act 不再有 wire**：pull 范式下 act 不唤醒 world。life 做完一件事直接
     ``insert_idempotent(ActPerformed)`` 落 PG，world 醒来按游标批量 pull。所以
     ``ActPerformed`` 没有任何出边（不 emit、不接 act_to_world_tick / world_act_wake，
     这两个节点已删）。
  3. ``EventArrived.debounce(key_by=event_knock_key) -> life_wake_node``：攒批
     唤醒 life（多条积压只醒一次）。
"""

from __future__ import annotations

import importlib

import pytest


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
    """加上 world/life event 的几条 wire 后，整图仍能编译（无 GraphError）。"""
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


def test_act_performed_has_no_wire():
    """pull 范式：ActPerformed 不再有任何出边（不唤醒 world）。

    act 落库但不 emit、不走 durable publish，所以 ``ActPerformed`` 在 wiring 里
    没有任何 wire（没有 act_to_world_tick / world_act_wake 这两个已删节点）。
    它纯粹是 world 醒来要 pull 的持久化状态、不是 push 事件。
    """
    _fresh_import()

    act_wires = _wires_for("ActPerformed")
    assert act_wires == [], (
        f"pull 范式下 ActPerformed 不该有任何 wire（act 不唤醒 world），实际 {act_wires}"
    )


def test_act_to_world_translation_nodes_deleted():
    """act→world 的唤醒节点 / 信号已彻底删除（无死引用）。"""
    _fresh_import()
    import app.world.engine as engine_mod

    for name in ("ActWorldTick", "act_to_world_tick", "world_act_wake", "act_wake_key"):
        assert not hasattr(engine_mod, name), (
            f"pull 范式下 {name} 应已从 engine 删除"
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


# ---------------------------------------------------------------------------
# world-driven wake —— life 自排执行腿（LifeWakeTick → life_self_wake_node）和被否的
# fan-out 心跳整套拆掉，唤醒只剩 world notify 一条腿（EventArrived → life_wake_node）。
# ---------------------------------------------------------------------------


def test_self_wake_and_heartbeat_signals_have_no_wire():
    """self 自排腿 + fan-out 心跳的信号在 wiring 里零残留（唤醒只剩 world notify）。"""
    _fresh_import()

    for name in ("LifeWakeTick", "LifeHeartbeatTick", "LifeHeartbeatSweep"):
        assert _wires_for(name) == [], (
            f"world-driven wake 下 {name} 不该有任何 wire（自排腿 / 心跳已拆）"
        )


def test_life_wake_machinery_deleted_from_module():
    """life_wake 模块不再导出 self 自排腿的任何符号（彻底删除、无死引用）。"""
    _fresh_import()
    import app.nodes.life_wake as lw

    for name in ("LifeWakeTick", "life_self_wake_node", "_life_self_wake_gate_passes"):
        assert not hasattr(lw, name), (
            f"world-driven wake 下 {name} 应已从 life_wake 删除"
        )


def test_life_heartbeat_module_deleted():
    """被否的 life fan-out 心跳模块整套删除（import 应失败）。"""
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.life.life_heartbeat")


def test_world_outline_registered_and_migratable_via_production_graph_load():
    """WorldOutline 通过【生产 graph load 路径】进 DATA_REGISTRY 且被 migrator 建表
    （codex T3 建议 3）——不靠测试里显式 migrate(WorldOutline)。

    线上建表口径：``load_dataflow_graph()`` 跑 ``app.wiring`` 的 side-effect import 链
    （→ world 节点 ``app.world.engine`` / ``app.world.tools`` → ``app.world.outline`` 的
    Data 注册），migrator 再从 ``DATA_REGISTRY`` 算 CREATE TABLE。所以「WorldOutline 进没
    进 DATA_REGISTRY」直接决定 ``data_world_outline`` 线上建不建。

    防回归：以后若把 outline 的生产 import 全拆了，整合测试里显式 ``migrate(WorldOutline)``
    仍会绿（它自己建表），但线上从 DATA_REGISTRY 算迁移时就漏了 ``data_world_outline`` →
    续写读大纲炸 ``UndefinedTableError``。这里走真生产 graph load + **按 class 名**断言
    （不 ``from app.world.outline import WorldOutline``，那样会绕过生产 import 自己把它注册
    进去、遮住回归），把这个口径钉死。不用 ``sys.modules.pop`` reimport hack。
    """
    from app.runtime.bootstrap import load_dataflow_graph
    from app.runtime.data import DATA_REGISTRY
    from app.runtime.migrator import plan_migration

    load_dataflow_graph()  # 生产 graph 构建：跑 app.wiring 的 side-effect import 链

    outline_cls = next(
        (c for c in DATA_REGISTRY if c.__name__ == "WorldOutline"), None
    )
    assert outline_cls is not None, (
        "WorldOutline 必须通过生产 graph load 注册进 DATA_REGISTRY，"
        "否则线上 migrate 不建 data_world_outline 表（显式 migrate 测试遮不住这个回归）"
    )

    # durable（非 transient）：migrator 必须为它产 CREATE TABLE data_world_outline，
    # 否则注册了也不建表（口径同 chat_wiring 的 table-in-migrator 断言）。
    plan = plan_migration([outline_cls], {})
    sql_blob = "\n".join(s.sql for s in plan.stmts)
    assert "data_world_outline" in sql_blob, (
        "WorldOutline 必须被 migrator 建表（data_world_outline），否则注册了线上也不建表"
    )
