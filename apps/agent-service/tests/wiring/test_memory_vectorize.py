"""wiring/memory_vectorize.py + deployment.py registration contract.

Inspects WIRING_REGISTRY + _BINDINGS after a fresh reload to confirm
the memory v4 vectorize topology:

    Source.mq("memory_fragment_vectorize") -> vectorize_memory_fragment
    Source.mq("memory_abstract_vectorize") -> vectorize_memory_abstract

both bound to the ``vectorize-worker`` app.
"""
from __future__ import annotations

import importlib


def _fresh_import():
    import app.deployment as d
    import app.wiring.memory as m
    import app.wiring.memory_vectorize as mv
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(m)
    importlib.reload(mv)
    importlib.reload(d)


def test_fragment_mq_wire_registered():
    _fresh_import()

    from app.domain.memory_request import MemoryFragmentRequest
    from app.nodes.memory_vectorize import vectorize_memory_fragment
    from app.runtime.wire import WIRING_REGISTRY

    wires = [w for w in WIRING_REGISTRY if w.data_type is MemoryFragmentRequest]
    assert any(
        vectorize_memory_fragment in w.consumers
        and any(
            s.kind == "mq" and s.params.get("queue") == "memory_fragment_vectorize"
            for s in w.sources
        )
        for w in wires
    )


def test_abstract_mq_wire_registered():
    _fresh_import()

    from app.domain.memory_request import MemoryAbstractRequest
    from app.nodes.memory_vectorize import vectorize_memory_abstract
    from app.runtime.wire import WIRING_REGISTRY

    wires = [w for w in WIRING_REGISTRY if w.data_type is MemoryAbstractRequest]
    assert any(
        vectorize_memory_abstract in w.consumers
        and any(
            s.kind == "mq" and s.params.get("queue") == "memory_abstract_vectorize"
            for s in w.sources
        )
        for w in wires
    )


def test_memory_vectorize_bindings():
    _fresh_import()

    from app.nodes.memory_vectorize import (
        vectorize_memory_abstract,
        vectorize_memory_fragment,
    )
    from app.runtime.placement import iter_bindings

    b = dict(iter_bindings())
    assert b[vectorize_memory_fragment] == "vectorize-worker"
    assert b[vectorize_memory_abstract] == "vectorize-worker"
