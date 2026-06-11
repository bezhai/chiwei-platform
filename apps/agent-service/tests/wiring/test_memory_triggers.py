"""wiring/memory_triggers.py registration contract.

Confirms the afterthought debounce wire is declared and that compile_graph()
accepts it. drift（voice 再生成）那条 wire 随 voice 子系统拆除一并删除，这里
负向断言它不再注册。
"""
from __future__ import annotations

import importlib


def _fresh_import():
    """Reload all wiring submodules against a clean registry.

    ``importlib.reload(wiring_pkg)`` only re-executes the package's
    ``__init__.py``. Python's import system short-circuits the
    ``from app.wiring import memory_triggers, ...`` line because
    the children already sit in ``sys.modules`` from the first import,
    so their module-level ``wire(...)`` calls do *not* re-fire. Reload
    every submodule explicitly to repopulate ``WIRING_REGISTRY``.
    """
    import app.deployment as deployment
    import app.wiring.memory_triggers as memory_triggers_mod
    import app.wiring.memory_vectorize as memory_vectorize_mod
    import app.wiring.safety as safety_mod
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(memory_triggers_mod)
    importlib.reload(memory_vectorize_mod)
    importlib.reload(safety_mod)
    importlib.reload(deployment)


def test_wiring_memory_triggers_compiles():
    """Importing app.wiring.memory_triggers must not break compile_graph()."""
    _fresh_import()

    from app.domain.memory_triggers import AfterthoughtTrigger
    from app.runtime.graph import compile_graph

    g = compile_graph()
    assert AfterthoughtTrigger in g.data_types


def test_drift_trigger_wire_gone():
    """voice 子系统拆除：DriftTrigger 类与它的 debounce wire 都不得残留。"""
    _fresh_import()

    import app.domain.memory_triggers as mt
    from app.runtime.wire import WIRING_REGISTRY

    assert not hasattr(mt, "DriftTrigger")
    leftover = [
        w for w in WIRING_REGISTRY if w.data_type.__name__ == "DriftTrigger"
    ]
    assert leftover == []


def test_afterthought_trigger_debounce_wire_registered():
    _fresh_import()

    from app.domain.memory_triggers import AfterthoughtTrigger
    from app.nodes.memory_pipelines import afterthought_check
    from app.runtime.wire import WIRING_REGISTRY

    wires = [w for w in WIRING_REGISTRY if w.data_type is AfterthoughtTrigger]
    assert len(wires) == 1
    w = wires[0]
    assert w.consumers == [afterthought_check]
    assert w.debounce == {"seconds": 300, "max_buffer": 15}
    sample = AfterthoughtTrigger(chat_id="oc_x", persona_id="p1")
    assert w.debounce_key_by(sample) == "afterthought:oc_x:p1"
