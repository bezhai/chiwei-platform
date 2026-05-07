"""Phase 6 v4 Gap 3: agent tool event wiring."""
from __future__ import annotations

import importlib


def _fresh_import():
    import app.deployment as d
    import app.wiring.agent_tool_events as w_evt
    import app.wiring.memory as w_mem
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(w_mem)
    importlib.reload(w_evt)
    importlib.reload(d)


def test_abstract_committed_wired_to_on_abstract_committed():
    _fresh_import()

    from app.domain.agent_tool_events import AbstractMemoryCommitted
    from app.nodes.memory_pipelines import on_abstract_committed
    from app.runtime.wire import WIRING_REGISTRY

    wires = [w for w in WIRING_REGISTRY if w.data_type is AbstractMemoryCommitted]
    assert len(wires) == 1
    assert on_abstract_committed in wires[0].consumers
    assert not wires[0].durable  # in-process re-emit


def test_compile_succeeds_with_new_wires():
    _fresh_import()

    from app.runtime.graph import compile_graph

    compile_graph()
