"""睡前回顾凌晨对账 cron 链路 wiring 契约.

照 fetch_dataflow 的三层翻译（时间源必须单字段 ts 的框架硬约束）：
  cron 0 5-10 * * *（Asia/Shanghai，清晨对账窗口逐小时一班）→ LifeDayReviewTick
  （单字段 ts）→ review_to_sweep_tick（补 lane）→ LifeDayReviewSweep
  → day_review_sweep_node。

只 inspect WIRING_REGISTRY + 断言全图能 compile；单字段 ts 契约由
``test_time_source_payload_contract.py`` 的参数化用例覆盖（已加载 review_dataflow）。
"""
from __future__ import annotations

import importlib


def _fresh_import():
    import app.wiring.review_dataflow as rd
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(rd)


def test_review_cron_drives_translation_node():
    """LifeDayReviewTick 由清晨 05:00–10:00 逐小时 cron（Asia/Shanghai）驱动，打到翻译节点。

    05:00 = 生活日晨界（04:00）之后一小时：对账「刚结束的生活日」，给快班留足
    当晚成功落 marker 的窗口。窗口逐小时（05–10 共六班）：05:00 那班失败还有
    五班补，已成功的由 marker 幂等挡住（窗口内每班 target 相同——
    previous_living_day 在 04:00 晨界后整个上午都是同一前一日标签）。
    钟纯机械、不依赖任何 agent 行为、部署杀不掉。
    """
    _fresh_import()

    from app.life.review_cron import review_to_sweep_tick
    from app.runtime.wire import WIRING_REGISTRY

    tick_wires = [
        w for w in WIRING_REGISTRY if w.data_type.__name__ == "LifeDayReviewTick"
    ]
    assert len(tick_wires) == 1
    w = tick_wires[0]
    assert w.consumers == [review_to_sweep_tick]
    assert len(w.sources) == 1
    src = w.sources[0]
    assert src.kind == "cron"
    assert src.params["expr"] == "0 5-10 * * *"
    assert src.params["tz"] == "Asia/Shanghai"


def test_review_sweep_is_in_process_to_sweep_node():
    """LifeDayReviewSweep 纯 in-process（无时间源），打到对账节点。"""
    _fresh_import()

    from app.life.review_cron import day_review_sweep_node
    from app.runtime.wire import WIRING_REGISTRY

    sweep_wires = [
        w for w in WIRING_REGISTRY if w.data_type.__name__ == "LifeDayReviewSweep"
    ]
    assert len(sweep_wires) == 1
    w = sweep_wires[0]
    assert w.consumers == [day_review_sweep_node]
    assert w.sources == [], "Sweep 不该直接挂时间源（单字段约束由 tick 承载）"


def test_review_dataflow_compiles_into_production_graph():
    """加载全部生产 wiring（含 review_dataflow）后图能 compile。"""
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    for sub in (
        "admin",
        "chat",
        "fetch_dataflow",
        "life_dataflow",
        "review_dataflow",
        "safety",
    ):
        importlib.reload(importlib.import_module(f"app.wiring.{sub}"))
    importlib.reload(importlib.import_module("app.wiring"))

    from app.runtime.graph import compile_graph

    graph = compile_graph()
    assert graph is not None
    types = {w.data_type.__name__ for w in graph.wires}
    assert {"LifeDayReviewTick", "LifeDayReviewSweep"} <= types
