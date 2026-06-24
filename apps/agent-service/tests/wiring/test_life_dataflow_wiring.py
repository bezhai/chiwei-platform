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


def test_life_dataflow_wire_count_is_5():
    _fresh_import()

    from app.runtime.wire import WIRING_REGISTRY

    # world-driven wake：角色的自排执行腿（LifeWakeTick → life_self_wake_node）和
    # 被否的 life fan-out 定时心跳（LifeHeartbeatTick / LifeHeartbeatSweep）整套拆掉，
    # 唤醒只剩 world notify 一条腿（EventArrived → life_wake_node）。活线：
    #   world/life event 闭环：WorldHeartbeatTick、WorldTick、EventArrived（3）
    #   备忘录 & 日程 第三块：ScheduleReminderTick（日程到点提醒 in-process 回环，1）
    #   读小说 Task 2：ReadingTriggered（durable，异步阅读任务，1）
    #   = 3 + 1 + 1 = 5。
    #
    # ScheduleReminderTick 是日程到点的独立唤醒一路（与本次拆除的 self-wake / 心跳
    # 无关，notebook 精确日程路径保留）：每条日程各挂各的提醒，到期经这条 in-process
    # 边接回 life_schedule_reminder_node。
    #
    # ReadingTriggered 是读小说 Task 2 的异步阅读触发（durable，跨进程）：她调 read_book
    # 工具认准一本书 → emit → reading_node 读一程揉印象。区别于 act 的 pull：读书是 push
    # 触发、有 wire。
    #
    # pull 范式：ActPerformed 不再有 wire（act 落 PG 不唤醒 world）、ActWorldTick 已删
    # （act→world 60s 合并闸整条链拆掉）。
    types = {w.data_type.__name__ for w in WIRING_REGISTRY}
    expected = {
        "WorldHeartbeatTick", "WorldTick", "EventArrived",
        "ScheduleReminderTick", "ReadingTriggered",
    }
    assert types == expected
    assert len(WIRING_REGISTRY) == 5


def test_self_wake_and_heartbeat_wires_gone():
    """world-driven wake：self 自排腿 + fan-out 心跳整套 wire 不得再有残留。"""
    _fresh_import()

    from app.runtime.wire import WIRING_REGISTRY

    leftover = [
        w.data_type.__name__
        for w in WIRING_REGISTRY
        if w.data_type.__name__ in (
            "LifeWakeTick", "LifeHeartbeatTick", "LifeHeartbeatSweep",
        )
    ]
    assert leftover == [], f"self-wake / heartbeat wires still registered: {leftover}"


def test_reviewer_wires_gone():
    """v4 reviewer 已删：cron tick 与 per-persona review 请求不得再有 wire。"""
    _fresh_import()

    from app.runtime.wire import WIRING_REGISTRY

    leftover = [
        w.data_type.__name__
        for w in WIRING_REGISTRY
        if w.data_type.__name__ in (
            "LightDayTick", "LightNightTick", "HeavyReviewTick",
            "LightReviewRequest", "HeavyReviewRequest",
        )
    ]
    assert leftover == [], f"reviewer wires still registered: {leftover}"


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
