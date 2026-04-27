from __future__ import annotations

from typing import Annotated

import pytest

from app.runtime.data import AdminOnly, Data, Key
from app.runtime.graph import GraphError, compile_graph
from app.runtime.node import node
from app.runtime.placement import bind, clear_bindings
from app.runtime.sink import Sink
from app.runtime.wire import clear_wiring, wire


class M(Data):
    mid: Annotated[str, Key]


class Cfg(Data, AdminOnly):
    cid: Annotated[str, Key]
    v: dict


class S(Data):
    sid: Annotated[str, Key]
    v: int


class X(Data):
    xid: Annotated[str, Key]


def setup_function():
    clear_wiring()
    clear_bindings()


def test_compile_success():
    @node
    async def f(m: M) -> None: ...

    wire(M).to(f)
    g = compile_graph()
    assert M in g.data_types
    assert f in g.nodes


def test_consumer_signature_mismatch_rejected():
    @node
    async def takes_m(m: M) -> None: ...

    # Wire declares M -> takes_m, consumer accepts M. Should pass.
    wire(M).to(takes_m)
    compile_graph()  # no error


def test_admin_only_consumer_ok():
    # AdminOnly can be consumed (read-only), just not produced.
    @node
    async def reads_cfg(c: Cfg) -> None: ...

    wire(Cfg).to(reads_cfg)
    compile_graph()  # ok


def test_wire_to_unknown_node_rejected():
    async def not_a_node(m: M) -> None: ...

    wire(M).to(not_a_node)
    with pytest.raises(GraphError, match="not registered"):
        compile_graph()


def test_with_latest_requires_as_latest_declared():
    # S is defined at module top so @node's get_type_hints can resolve the annotation.
    @node
    async def f(m: M, s: S) -> None: ...

    wire(M).to(f).with_latest(S)
    # S has no wire(S).as_latest() declaration anywhere
    with pytest.raises(GraphError, match="with_latest.*requires.*as_latest"):
        compile_graph()


def test_consumer_missing_data_type_param_rejected():
    # Consumer only accepts Cfg, but wire routes M to it -> signature mismatch.
    @node
    async def wrong(c: Cfg) -> None: ...

    wire(M).to(wrong)
    with pytest.raises(GraphError, match="does not accept"):
        compile_graph()


def test_consumer_missing_with_latest_param_rejected():
    # Consumer accepts M but not S; wire asks for with_latest(S) -> signature mismatch.
    @node
    async def takes_only_m(m: M) -> None: ...

    @node
    async def s_producer(s: S) -> None: ...

    wire(S).to(s_producer).as_latest()
    wire(M).to(takes_only_m).with_latest(S)
    with pytest.raises(GraphError, match="does not accept"):
        compile_graph()


def test_layer4_rejects_wire_with_consumers_in_different_apps():
    # Two consumers on the same wire, each bound to a different app ->
    # compile_graph() must refuse. Otherwise ``start_consumers(app_name)``
    # would silently drop one side at runtime.
    @node
    async def worker_consumer(x: X) -> None: ...

    @node
    async def main_consumer(x: X) -> None: ...

    wire(X).to(worker_consumer, main_consumer)
    bind(worker_consumer).to_app("vectorize-worker")
    bind(main_consumer).to_app("agent-service")

    with pytest.raises(GraphError, match="mixed apps"):
        compile_graph()


def test_debounce_rejected_until_engine_supports_it():
    # Surface exists for the DSL/typing story but the engine doesn't
    # dispatch debounce yet; using it must fail loudly at startup.
    @node
    async def f(m: M) -> None: ...

    wire(M).to(f).debounce(seconds=10, max_buffer=5)
    with pytest.raises(GraphError, match="debounce.*not yet"):
        compile_graph()


def test_sink_rejected_until_engine_dispatches():
    # Sink.mq is exposed so wire-level docs/tests can talk about it,
    # but the engine has no sink dispatch yet — wiring it up must
    # raise rather than silently no-op the publish.
    @node
    async def f(m: M) -> None: ...

    wire(M).to(f, Sink.mq("test_queue"))
    with pytest.raises(GraphError, match="Sink.*not dispatched|sinks are not"):
        compile_graph()
