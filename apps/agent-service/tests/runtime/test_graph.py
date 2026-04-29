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


class TMsg(Data):
    """Transient Data: no pg table. Used by the durable+transient mutual-exclusion test."""

    tid: Annotated[str, Key]

    class Meta:
        transient = True


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
    with pytest.raises(GraphError, match="does not match the wire inputs"):
        compile_graph()


def test_consumer_missing_with_latest_param_rejected():
    # Consumer accepts M but not S; wire asks for with_latest(S) -> signature mismatch.
    @node
    async def takes_only_m(m: M) -> None: ...

    @node
    async def s_producer(s: S) -> None: ...

    wire(S).to(s_producer).as_latest()
    wire(M).to(takes_only_m).with_latest(S)
    with pytest.raises(GraphError, match="does not match the wire inputs"):
        compile_graph()


def test_consumer_extra_data_param_rejected():
    # Consumer takes M and X; wire only declares M (no with_latest(X)).
    # Subset matching used to pass this — emit() then crashes with a
    # missing-kwarg at first traffic. compile_graph must reject it at boot.
    @node
    async def takes_extra(m: M, x: X) -> None: ...

    wire(M).to(takes_extra)
    with pytest.raises(GraphError, match="extra .*X"):
        compile_graph()


def test_consumer_in_two_wires_rejected():
    # Strict equality enforces 1-consumer-1-wire. A function reused
    # across wires has more params than any single wire's needed set,
    # so both wires fail signature equality.
    @node
    async def shared(m: M, x: X) -> None: ...

    wire(M).to(shared)
    wire(X).to(shared)
    with pytest.raises(GraphError, match="appear on exactly one wire|does not match the wire inputs"):
        compile_graph()


def test_default_bound_and_unbound_on_same_wire_ok():
    # Two consumers on the same wire: one explicitly bound to
    # DEFAULT_APP, one unbound. nodes_for_app(DEFAULT_APP) treats
    # unbound as default — so this is *not* a mixed-app wire at
    # runtime. compile_graph must reflect that semantic and accept it.
    from app.runtime.placement import DEFAULT_APP

    @node
    async def explicit_default(m: M) -> None: ...

    @node
    async def implicit_default(m: M) -> None: ...

    bind(explicit_default).to_app(DEFAULT_APP)
    wire(M).to(explicit_default, implicit_default)
    compile_graph()  # no raise


def test_durable_with_latest_rejected_until_handler_supports_it():
    # Durable consumer dispatch is single-input: publish_durable only
    # carries the primary Data on the queue, and _build_handler calls
    # the consumer with one kwarg. with_latest is resolved only on the
    # in-process emit path. Combining them used to compile fine and
    # then explode on first delivery — refuse at boot.
    @node
    async def takes_m_with_s(m: M, s: S) -> None: ...

    @node
    async def s_producer(s: S) -> None: ...

    wire(S).to(s_producer).as_latest()
    wire(M).to(takes_m_with_s).with_latest(S).durable()
    with pytest.raises(GraphError, match="durable.*with_latest|with_latest.*durable"):
        compile_graph()


def test_durable_transient_data_rejected():
    # Meta.transient = True means no pg table; durable consumers call
    # insert_idempotent which writes to that table — so the combo only
    # works as far as the queue, then the consumer crashes on first
    # message. Reject at compile time so the failure isn't deferred to
    # the first inflight delivery.
    @node
    async def consumer(t: TMsg) -> None: ...

    wire(TMsg).to(consumer).durable()
    with pytest.raises(GraphError, match="transient.*durable|durable.*transient"):
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

    wire(M).to(f).debounce(seconds=10, max_buffer=5, key_by=lambda m: m.mid)
    with pytest.raises(GraphError, match="debounce.*not yet"):
        compile_graph()


def test_compile_graph_accepts_wire_with_sink_mq_in_all_routes():
    """Sink.mq("recall") is in ALL_ROUTES → compile_graph accepts it."""
    @node
    async def f(m: M) -> None: ...

    wire(M).to(f, Sink.mq("recall"))  # recall is in ALL_ROUTES

    g = compile_graph()
    assert any(s.kind == "mq" for w in g.wires for s in w.sinks)


def test_compile_graph_rejects_sink_mq_with_unknown_queue():
    """Sink.mq("not_in_routes") raises at compile time."""
    @node
    async def f(m: M) -> None: ...

    wire(M).to(f, Sink.mq("not_in_routes"))

    with pytest.raises(GraphError) as excinfo:
        compile_graph()
    assert "not_in_routes" in str(excinfo.value)
    assert "ALL_ROUTES" in str(excinfo.value)


def test_http_source_consumer_must_be_in_default_app():
    # register_http_sources() mounts FastAPI routes only on the main
    # (agent-service) process. A consumer bound to a worker app would
    # see the request return 202 to the caller while emit() filters it
    # out by APP_NAME — silent drop. compile_graph must reject this at
    # boot.
    from app.runtime.source import Source

    @node
    async def worker_only(m: M) -> None: ...

    bind(worker_only).to_app("vectorize-worker")
    wire(M).to(worker_only).from_(Source.http("/api/trigger"))

    with pytest.raises(GraphError, match="HTTP sources are mounted only"):
        compile_graph()


def test_http_source_consumer_in_default_app_ok():
    # Default-app (unbound) consumer is fine.
    from app.runtime.source import Source

    @node
    async def main_handler(m: M) -> None: ...

    wire(M).to(main_handler).from_(Source.http("/api/trigger"))
    compile_graph()  # no raise
