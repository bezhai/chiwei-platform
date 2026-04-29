"""wiring/memory_triggers.py registration contract.

Confirms the Phase 3 debounce wires are declared and that compile_graph()
accepts them — DriftTrigger / AfterthoughtTrigger both reach the consumer
side of WIRING_REGISTRY after a fresh wiring import, and CompiledGraph
exposes them via .data_types.
"""
from __future__ import annotations

import importlib


def _fresh_import():
    """Reload all wiring submodules against a clean registry.

    ``importlib.reload(wiring_pkg)`` only re-executes the package's
    ``__init__.py``. Python's import system short-circuits the
    ``from app.wiring import memory, memory_triggers, ...`` line because
    the children already sit in ``sys.modules`` from the first import,
    so their module-level ``wire(...)`` calls do *not* re-fire. Reload
    every submodule explicitly to repopulate ``WIRING_REGISTRY``.
    """
    import app.deployment as deployment
    import app.wiring.memory as memory_mod
    import app.wiring.memory_triggers as memory_triggers_mod
    import app.wiring.memory_vectorize as memory_vectorize_mod
    import app.wiring.safety as safety_mod
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(memory_mod)
    importlib.reload(memory_triggers_mod)
    importlib.reload(memory_vectorize_mod)
    importlib.reload(safety_mod)
    importlib.reload(deployment)


def test_wiring_memory_triggers_compiles():
    """Importing app.wiring.memory_triggers must not break compile_graph()."""
    _fresh_import()

    from app.domain.memory_triggers import AfterthoughtTrigger, DriftTrigger
    from app.runtime.graph import compile_graph

    g = compile_graph()
    assert DriftTrigger in g.data_types
    assert AfterthoughtTrigger in g.data_types


def test_drift_trigger_debounce_wire_registered():
    _fresh_import()

    from app.domain.memory_triggers import DriftTrigger
    from app.nodes.memory_pipelines import drift_check
    from app.runtime.wire import WIRING_REGISTRY

    wires = [w for w in WIRING_REGISTRY if w.data_type is DriftTrigger]
    assert len(wires) == 1
    w = wires[0]
    assert w.consumers == [drift_check]
    assert w.debounce is not None
    assert w.debounce_key_by is not None
    # Sanity: key formula contains the chat_id + persona_id and the
    # ``drift:`` prefix that distinguishes from afterthought.
    sample = DriftTrigger(chat_id="oc_x", persona_id="p1")
    assert w.debounce_key_by(sample) == "drift:oc_x:p1"


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
