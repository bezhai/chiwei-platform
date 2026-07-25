"""persona review 每日补班 cron 链路 wiring 契约.

照 fetch_dataflow / review_dataflow 的三层翻译（时间源必须单字段 ts 的框架硬约束）：
  cron 0 11 * * *（Asia/Shanghai，每天 11:00 一班，避开睡前回顾 05–10 对账窗口）
  → PersonaReviewTick（单字段 ts）→ persona_review_to_sweep_tick（补 lane）
  → PersonaReviewSweep → persona_review_sweep_node。

只 inspect WIRING_REGISTRY + 断言全图能 compile；单字段 ts 契约由
``test_time_source_payload_contract.py`` 的参数化用例覆盖（已加载
persona_review_dataflow）。
"""
from __future__ import annotations

import importlib


def _fresh_import():
    import app.wiring.persona_review_dataflow as prd
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(prd)


def test_persona_review_cron_drives_translation_node():
    """PersonaReviewTick 由每天 11:00（Asia/Shanghai）cron 驱动，打到翻译节点。

    11:00 与睡前回顾 05:00–10:00 的对账窗口错开；周级幂等由 sweep 节点的预检 +
    run 锁内复查保证（本周已有 review 版的班空转跳过），失败的班次日自动补。
    """
    _fresh_import()

    from app.life.persona_review_cron import persona_review_to_sweep_tick
    from app.runtime.wire import WIRING_REGISTRY

    tick_wires = [
        w for w in WIRING_REGISTRY if w.data_type.__name__ == "PersonaReviewTick"
    ]
    assert len(tick_wires) == 1
    w = tick_wires[0]
    assert w.consumers == [persona_review_to_sweep_tick]
    assert len(w.sources) == 1
    src = w.sources[0]
    assert src.kind == "cron"
    assert src.params["expr"] == "0 11 * * *"
    assert src.params["tz"] == "Asia/Shanghai"


def test_persona_review_sweep_is_in_process_to_sweep_node():
    """PersonaReviewSweep 纯 in-process（无时间源），打到 sweep 节点。"""
    _fresh_import()

    from app.life.persona_review_cron import persona_review_sweep_node
    from app.runtime.wire import WIRING_REGISTRY

    sweep_wires = [
        w for w in WIRING_REGISTRY if w.data_type.__name__ == "PersonaReviewSweep"
    ]
    assert len(sweep_wires) == 1
    w = sweep_wires[0]
    assert w.consumers == [persona_review_sweep_node]
    assert w.sources == [], "Sweep 不该直接挂时间源（单字段约束由 tick 承载）"


def test_persona_review_dataflow_compiles_into_production_graph():
    """加载全部生产 wiring（含 persona_review_dataflow）后图能 compile。"""
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    for sub in (
        "admin",
        "chat",
        "fetch_dataflow",
        "life_dataflow",
        "persona_review_dataflow",
        "review_dataflow",
        "safety",
    ):
        importlib.reload(importlib.import_module(f"app.wiring.{sub}"))
    importlib.reload(importlib.import_module("app.wiring"))

    from app.runtime.graph import compile_graph

    graph = compile_graph()
    assert graph is not None
    types = {w.data_type.__name__ for w in graph.wires}
    assert {"PersonaReviewTick", "PersonaReviewSweep"} <= types
