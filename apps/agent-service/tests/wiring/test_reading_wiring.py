"""异步阅读 wiring 验收 — 读小说 Task 2.

她在 life 轮调 read_book 工具 → emit 一个 durable ``ReadingTriggered`` → 必须有一条
**durable** wire 把它接到 ``reading_node`` 让异步阅读任务在另一进程消费（life 进程 emit、
阅读 @node 进程消费，仿 act 落 PG 跨进程；但读书是 push 触发、有 wire，区别于 act 的 pull
无 wire）。
"""

from __future__ import annotations

import importlib


def _fresh_import():
    """重新加载 life_dataflow wiring，让 wire(...) 语句重跑、WIRING_REGISTRY 从头填。"""
    import app.wiring.life_dataflow as ld
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(ld)


def _wires_for(name: str):
    from app.runtime.wire import WIRING_REGISTRY

    return [w for w in WIRING_REGISTRY if w.data_type.__name__ == name]


def test_reading_triggered_wired_to_reading_node():
    """ReadingTriggered → reading_node 有一条 wire（异步阅读任务被消费）。"""
    _fresh_import()
    from app.nodes.reading import reading_node

    wires = _wires_for("ReadingTriggered")
    consumer_wires = [w for w in wires if reading_node in w.consumers]
    assert consumer_wires, "ReadingTriggered 没有接到 reading_node"


def test_reading_triggered_wire_is_durable():
    """这条 wire 是 durable（跨进程：life emit、阅读 @node 进程消费，不丢）。"""
    _fresh_import()
    from app.nodes.reading import reading_node

    consumer_wires = [
        w for w in _wires_for("ReadingTriggered") if reading_node in w.consumers
    ]
    assert any(w.durable for w in consumer_wires), (
        "ReadingTriggered → reading_node 必须 durable（跨进程消费、立即 emit 非定时器）"
    )


def test_graph_compiles_with_reading_wire():
    """加上读书 wire 后整图仍能编译（无 GraphError）。"""
    _fresh_import()
    from app.runtime.graph import compile_graph

    assert compile_graph() is not None
