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


def test_life_dataflow_wire_count_is_11():
    _fresh_import()

    from app.runtime.wire import WIRING_REGISTRY

    # 删旧 life tick / glimpse / schedule 生成 wire 后、pull 范式删 act 唤醒链后剩：
    #   cron：MinuteTick（只剩 fan_out_voice 这半）、LightDayTick、LightNightTick、
    #     HeavyReviewTick（4）
    #   per-persona business：VoiceRequest、LightReviewRequest、HeavyReviewRequest（3）
    #   world/life event 闭环：WorldHeartbeatTick、WorldTick、EventArrived（3）
    #   阶段 1B Task 2：LifeWakeTick（life 自排 in-process 回环那条边，1）
    #   = 4 + 3 + 3 + 1 = 11。
    #
    # pull 范式：ActPerformed 不再有 wire（act 落 PG 不唤醒 world）、ActWorldTick 已删
    # （act→world 60s 合并闸整条链拆掉），相比旧版少了这两条。
    types = {w.data_type.__name__ for w in WIRING_REGISTRY}
    expected = {
        "MinuteTick", "LightDayTick", "LightNightTick", "HeavyReviewTick",
        "VoiceRequest", "LightReviewRequest", "HeavyReviewRequest",
        "WorldHeartbeatTick", "WorldTick", "EventArrived",
        "LifeWakeTick",
    }
    assert types == expected
    assert len(WIRING_REGISTRY) == 11


def test_minute_tick_drives_voice_fan_out():
    """旧 fan_out_life_tick 已删，MinuteTick 这条 wire 只保留 voice 这半。"""
    _fresh_import()

    from app.nodes.life_dataflow import fan_out_voice
    from app.runtime.wire import WIRING_REGISTRY

    minute_wires = [w for w in WIRING_REGISTRY if w.data_type.__name__ == "MinuteTick"]
    assert len(minute_wires) == 1
    assert minute_wires[0].consumers == [fan_out_voice]


def test_act_performed_has_no_wire():
    """pull 范式：ActPerformed 不再有 wire（act 落 PG 不唤醒 world）。"""
    _fresh_import()

    from app.runtime.wire import WIRING_REGISTRY

    act_wires = [w for w in WIRING_REGISTRY if w.data_type.__name__ == "ActPerformed"]
    assert act_wires == []
