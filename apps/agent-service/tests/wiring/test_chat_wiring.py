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


def test_chat_nodes_placement_under_agent_service():
    """route_chat_node + chat_node 必须落在 agent-service 默认 app。

    emit() 用 nodes_for_app(APP_NAME) 过滤 in-process consumer：proactive
    在 agent-service 进程 emit ChatTrigger 后 route_chat_node 必须在
    agent-service 这一组 nodes 里，否则 fan-out 会被静默跳过。chat_node
    同理。

    deployment.py 没显式 bind 这两个节点 → fall through 到 default app
    "agent-service"。如果未来一改 bind（误绑到 vectorize-worker），这条
    测试会立刻挂掉。
    """
    _fresh_import()

    # 让 deployment.py 的 bind 注册重新生效。需要先 import（首次 import 会
    # 触发 body 注册 bindings）+ clear_bindings + reload（重新执行 body）。
    # 直接 reload 而不 pre-clear 会因 _BINDINGS 残留触发 "already bound"
    # raise（_Binder.to_app 检测重复绑定）。
    import app.deployment as d  # noqa: F401
    from app.runtime.placement import clear_bindings, nodes_for_app

    clear_bindings()
    importlib.reload(d)

    from app.nodes.chat_node import chat_node, route_chat_node

    agent_service_nodes = nodes_for_app("agent-service")
    assert route_chat_node in agent_service_nodes, (
        "route_chat_node must be in agent-service app; emit(ChatTrigger) "
        "from same process won't reach it otherwise"
    )
    assert chat_node in agent_service_nodes, (
        "chat_node must be in agent-service app"
    )
