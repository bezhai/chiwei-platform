"""Phase 4 life_dataflow wiring smoke test."""
from __future__ import annotations

import importlib


def _fresh_import():
    """Repopulate WIRING_REGISTRY from scratch by reloading life_dataflow.

    Matches the pattern used by test_safety_wiring.py: clear registries, then reload
    to force re-execution of wire statements.
    """
    import app.wiring.life_dataflow as ld
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(ld)


def test_life_dataflow_wiring_compiles():
    """Loading the wiring module must produce a graph that compiles."""
    _fresh_import()

    from app.runtime.graph import compile_graph

    graph = compile_graph()  # raises GraphError on misconfig
    assert graph is not None


def test_life_dataflow_wire_count_is_9():
    _fresh_import()

    from app.runtime.wire import WIRING_REGISTRY

    # 删旧 life tick / glimpse / schedule 生成 wire 后、pull 范式删 act 唤醒链后、
    # voice 子系统拆除（MinuteTick cron + VoiceRequest fan-out 两条）后剩：
    #   cron：LightDayTick、LightNightTick、HeavyReviewTick（3）
    #   per-persona business：LightReviewRequest、HeavyReviewRequest（2）
    #   world/life event 闭环：WorldHeartbeatTick、WorldTick、EventArrived（3）
    #   阶段 1B Task 2：LifeWakeTick（life 自排 in-process 回环那条边，1）
    #   = 3 + 2 + 3 + 1 = 9。
    #
    # pull 范式：ActPerformed 不再有 wire（act 落 PG 不唤醒 world）、ActWorldTick 已删
    # （act→world 60s 合并闸整条链拆掉）。
    types = {w.data_type.__name__ for w in WIRING_REGISTRY}
    expected = {
        "LightDayTick", "LightNightTick", "HeavyReviewTick",
        "LightReviewRequest", "HeavyReviewRequest",
        "WorldHeartbeatTick", "WorldTick", "EventArrived",
        "LifeWakeTick",
    }
    assert types == expected
    assert len(WIRING_REGISTRY) == 9


def test_voice_wires_gone():
    """voice 子系统已拆除：MinuteTick cron 与 VoiceRequest fan-out 不得再有 wire。"""
    _fresh_import()

    from app.runtime.wire import WIRING_REGISTRY

    leftover = [
        w.data_type.__name__
        for w in WIRING_REGISTRY
        if w.data_type.__name__ in ("MinuteTick", "VoiceRequest")
    ]
    assert leftover == [], f"voice wires still registered: {leftover}"


def test_act_performed_has_no_wire():
    """pull 范式：ActPerformed 不再有 wire（act 落 PG 不唤醒 world）。"""
    _fresh_import()

    from app.runtime.wire import WIRING_REGISTRY

    act_wires = [w for w in WIRING_REGISTRY if w.data_type.__name__ == "ActPerformed"]
    assert act_wires == []
