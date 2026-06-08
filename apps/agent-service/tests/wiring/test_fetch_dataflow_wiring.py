"""每日抓取 cron 链路 wiring 契约（刀 3 Task2）。

照搬 world heartbeat 的三层翻译解决「时间源必须单字段 ts」的框架硬约束：
  cron 30 19 * * * → DailyMaterialsTick（单字段 ts）→ fetch_to_materials_tick（补 lane）
    → DailyMaterialsFetch → daily_fetch_node。

这些测试不跑引擎，只 inspect WIRING_REGISTRY + 断言图能 compile。单字段 ts 契约由
``test_time_source_payload_contract.py`` 的参数化用例覆盖（它现在也加载 fetch_dataflow）。
"""
from __future__ import annotations

import importlib


def _fresh_import():
    """清空 registry 后 reload fetch_dataflow，强制 wire(...) 重新注册。

    照搬 test_life_dataflow_wiring.py 的 fresh-import 模式。
    """
    import app.wiring.fetch_dataflow as fd
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(fd)


def test_fetch_cron_drives_translation_node():
    """DailyMaterialsTick 由 cron 19:30 Asia/Shanghai 源驱动，打到翻译节点。"""
    _fresh_import()

    from app.fetch.node import fetch_to_materials_tick
    from app.runtime.wire import WIRING_REGISTRY

    tick_wires = [
        w for w in WIRING_REGISTRY if w.data_type.__name__ == "DailyMaterialsTick"
    ]
    assert len(tick_wires) == 1
    w = tick_wires[0]
    assert w.consumers == [fetch_to_materials_tick]
    assert len(w.sources) == 1
    src = w.sources[0]
    assert src.kind == "cron"
    assert src.params["expr"] == "30 19 * * *"
    assert src.params["tz"] == "Asia/Shanghai"


def test_fetch_signal_is_in_process_to_fetch_node():
    """DailyMaterialsFetch 纯 in-process（无时间源），打到 daily_fetch_node。"""
    _fresh_import()

    from app.fetch.node import daily_fetch_node
    from app.runtime.wire import WIRING_REGISTRY

    fetch_wires = [
        w for w in WIRING_REGISTRY if w.data_type.__name__ == "DailyMaterialsFetch"
    ]
    assert len(fetch_wires) == 1
    w = fetch_wires[0]
    assert w.consumers == [daily_fetch_node]
    assert w.sources == [], "DailyMaterialsFetch 不该直接挂时间源（单字段约束由 tick 承载）"


def test_fetch_dataflow_compiles_into_production_graph():
    """加载全部生产 wiring（含 fetch_dataflow）后图能 compile（不 misconfig）。"""
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    for sub in (
        "admin",
        "agent_tool_events",
        "chat",
        "fetch_dataflow",
        "life_dataflow",
        "memory",
        "memory_triggers",
        "memory_vectorize",
        "safety",
    ):
        importlib.reload(importlib.import_module(f"app.wiring.{sub}"))
    importlib.reload(importlib.import_module("app.wiring"))

    from app.runtime.graph import compile_graph

    graph = compile_graph()  # raises GraphError on misconfig
    assert graph is not None
    types = {w.data_type.__name__ for w in graph.wires}
    assert {"DailyMaterialsTick", "DailyMaterialsFetch"} <= types
