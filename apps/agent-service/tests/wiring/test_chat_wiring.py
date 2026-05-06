"""Tests for wiring/chat.py — Phase 5a chat pipeline wiring registration.

Asserts the three chat wires register correctly in WIRING_REGISTRY:

  Source.mq("chat_request")  -> route_chat_node          (ChatTrigger,        in-process)
                              -> chat_node                (ChatRequest,        durable)
                              -> Sink.mq("chat_response") (ChatResponseSegment, in-process)

Reload trick mirrors test_safety_wiring.py / test_memory.py: the autouse
``_reset_runtime_registries`` fixture in tests/conftest.py clears
WIRING_REGISTRY before each test, then ``importlib.reload`` re-runs the
module body so wire(...) calls repopulate the registry.
"""
from __future__ import annotations

import importlib


def _fresh_import():
    """Re-execute app.wiring.chat so wire(...) calls populate WIRING_REGISTRY.

    Conftest's autouse fixture has already called clear_wiring() — we just
    need to force the module body to run again. importlib.reload bypasses
    the import cache.
    """
    import app.wiring.chat as c
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(c)


def test_chat_trigger_wire_from_mq():
    """ChatTrigger wire pulls from Source.mq("chat_request") and feeds route_chat_node."""
    _fresh_import()

    from app.domain.chat_dataflow import ChatTrigger
    from app.nodes.chat_node import route_chat_node
    from app.runtime.wire import WIRING_REGISTRY

    wires = [w for w in WIRING_REGISTRY if w.data_type is ChatTrigger]
    assert any(
        route_chat_node in w.consumers
        and w.durable is False
        and any(s.kind == "mq" and s.params.get("queue") == "chat_request" for s in w.sources)
        for w in wires
    )


def test_chat_request_wire_durable_to_chat_node():
    """ChatRequest wire is durable and feeds chat_node."""
    _fresh_import()

    from app.domain.chat_dataflow import ChatRequest
    from app.nodes.chat_node import chat_node
    from app.runtime.wire import WIRING_REGISTRY

    wires = [w for w in WIRING_REGISTRY if w.data_type is ChatRequest]
    assert any(chat_node in w.consumers and w.durable is True for w in wires)


def test_chat_response_segment_wire_to_sink_mq():
    """ChatResponseSegment wire publishes to Sink.mq("chat_response")."""
    _fresh_import()

    from app.domain.chat_dataflow import ChatResponseSegment
    from app.runtime.sink import SinkSpec
    from app.runtime.wire import WIRING_REGISTRY

    wires = [w for w in WIRING_REGISTRY if w.data_type is ChatResponseSegment]
    assert any(
        any(
            isinstance(s, SinkSpec) and s.kind == "mq" and s.params.get("queue") == "chat_response"
            for s in w.sinks
        )
        for w in wires
    )


def test_chat_wiring_compiles():
    """Loading chat wiring lets compile_graph succeed end-to-end."""
    _fresh_import()

    from app.runtime.graph import compile_graph

    g = compile_graph()
    assert g is not None


def test_chat_request_table_in_migrator():
    """ChatRequest (transient=False) -> migrator emits CREATE TABLE.
    ChatTrigger / ChatResponseSegment (transient=True) -> migrator skips them.

    Pass only the three chat Data classes to plan_migration to keep the
    assertion focused — DATA_REGISTRY is a global set that other tests
    pollute with Data subclasses that don't all map cleanly to pg.
    """
    _fresh_import()

    from app.domain.chat_dataflow import ChatRequest, ChatResponseSegment, ChatTrigger
    from app.runtime.data import DATA_REGISTRY
    from app.runtime.migrator import plan_migration

    # All three classes were registered when chat_dataflow imported.
    assert ChatTrigger in DATA_REGISTRY
    assert ChatRequest in DATA_REGISTRY
    assert ChatResponseSegment in DATA_REGISTRY

    plan = plan_migration([ChatTrigger, ChatRequest, ChatResponseSegment], {})
    sql_blob = "\n".join(s.sql for s in plan.stmts)
    assert "data_chat_request" in sql_blob
    assert "data_chat_trigger" not in sql_blob
    assert "data_chat_response_segment" not in sql_blob
