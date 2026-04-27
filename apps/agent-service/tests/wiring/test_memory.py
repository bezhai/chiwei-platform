"""wiring/memory.py + deployment.py registration contract.

These tests don't run the engine — they inspect ``WIRING_REGISTRY`` and
the placement binding map after re-importing the two modules, to assert
that the declarative wire/bind calls produce the expected topology:

    Source.mq("vectorize") -> hydrate_message      (MessageRequest)
                    |
                 Message (durable)
                    v
                vectorize
                    |
                 Fragment (in-process)
                    v
               save_fragment

    all three @nodes bound to the "vectorize-worker" app.

Reload trick: both registries (WIRING_REGISTRY list, _BINDINGS dict) are
module-level mutables in app.runtime. We call the public clear_* helpers
to empty them, then ``importlib.reload(m)`` re-executes the memory/
deployment module bodies which call ``wire(...)`` / ``bind(...)`` again
and repopulate the registries. Each test starts from a clean slate.
"""
from __future__ import annotations

import importlib


def _fresh_import():
    """Repopulate WIRING_REGISTRY / _BINDINGS from scratch.

    Order matters: clear the registries *immediately before* reloading so
    the module bodies append into empty state. If we cleared first and
    let ``import`` run normally, Python's import cache would skip the
    side-effect re-execution. ``importlib.reload`` forces the body to
    run again, which is what we need.
    """
    import app.deployment as d
    import app.wiring.memory as m
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(m)
    importlib.reload(d)


def test_mq_entry_wired_to_hydrate():
    _fresh_import()

    from app.domain.message_request import MessageRequest
    from app.nodes.hydrate_message import hydrate_message
    from app.runtime.wire import WIRING_REGISTRY

    wires = [w for w in WIRING_REGISTRY if w.data_type is MessageRequest]
    assert any(
        hydrate_message in w.consumers
        and any(s.kind == "mq" and s.params.get("queue") == "vectorize" for s in w.sources)
        for w in wires
    )


def test_message_durable_to_vectorize():
    _fresh_import()

    from app.domain.message import Message
    from app.nodes.vectorize import vectorize
    from app.runtime.wire import WIRING_REGISTRY

    wires = [w for w in WIRING_REGISTRY if w.data_type is Message]
    assert any(w.durable and vectorize in w.consumers for w in wires)


def test_fragment_to_save_fragment():
    _fresh_import()

    from app.domain.fragment import Fragment
    from app.nodes.save_fragment import save_fragment
    from app.runtime.wire import WIRING_REGISTRY

    wires = [w for w in WIRING_REGISTRY if w.data_type is Fragment]
    assert any(save_fragment in w.consumers for w in wires)


def test_bindings_set():
    _fresh_import()

    from app.nodes.hydrate_message import hydrate_message
    from app.nodes.save_fragment import save_fragment
    from app.nodes.vectorize import vectorize
    from app.runtime.placement import iter_bindings

    b = dict(iter_bindings())
    assert b[hydrate_message] == "vectorize-worker"
    assert b[vectorize] == "vectorize-worker"
    assert b[save_fragment] == "vectorize-worker"


def test_compile_succeeds():
    _fresh_import()

    from app.runtime.graph import compile_graph

    compile_graph()
