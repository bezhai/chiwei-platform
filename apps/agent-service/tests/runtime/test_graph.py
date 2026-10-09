from __future__ import annotations

from typing import Annotated

import pytest

from app.runtime.data import AdminOnly, Data, Key
from app.runtime.graph import GraphError, compile_graph
from app.runtime.node import node
from app.runtime.sink import Sink
from app.runtime.wire import clear_wiring, wire


class M(Data):
    mid: Annotated[str, Key]


class Cfg(Data, AdminOnly):
    cid: Annotated[str, Key]
    v: dict


class X(Data):
    xid: Annotated[str, Key]


class ChannelledM(Data):
    """按 channel 分区的出站队列要求 Data 自己说得出 channel。"""

    mid: Annotated[str, Key]
    channel: str = "lark"


class TMsg(Data):
    """Transient Data: no pg table. Used by the durable+transient mutual-exclusion test."""

    tid: Annotated[str, Key]

    class Meta:
        transient = True


def setup_function():
    clear_wiring()


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


def test_consumer_missing_data_type_param_rejected():
    # Consumer only accepts Cfg, but wire routes M to it -> signature mismatch.
    @node
    async def wrong(c: Cfg) -> None: ...

    wire(M).to(wrong)
    with pytest.raises(GraphError, match="does not match the wire inputs"):
        compile_graph()


def test_consumer_extra_data_param_rejected():
    # Consumer takes M and X; wire only declares M.
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


def test_compile_graph_accepts_wire_with_sink_mq_in_all_routes():
    """A queue名 in ALL_ROUTES → compile_graph accepts it.

    用一条**不按 channel 分区**的队列：分区队列的 sink 还要求 Data 带 channel
    （见下一条用例），那是另一件事。
    """
    @node
    async def f(m: M) -> None: ...

    wire(M).to(f, Sink.mq("chat_response_lark"))

    g = compile_graph()
    assert any(s.kind == "mq" for w in g.wires for s in w.sinks)


def test_compile_graph_rejects_channel_partitioned_sink_on_channel_less_data():
    """chat_response / recall 按 channel 分区，Data 不带 channel 就无从分流。

    没有这条守卫的话，错误要等到第一条真实消息 emit 时才在 dispatch 里炸 —— 那时
    已经在 prod。
    """
    @node
    async def f(m: M) -> None: ...

    wire(M).to(f, Sink.mq("chat_response"))  # M 没有 channel 字段

    with pytest.raises(GraphError) as excinfo:
        compile_graph()
    assert "chat_response" in str(excinfo.value)
    assert "channel" in str(excinfo.value)


def test_compile_graph_accepts_channel_partitioned_sink_when_data_has_channel():
    @node
    async def f(c: ChannelledM) -> None: ...

    wire(ChannelledM).to(f, Sink.mq("recall"))

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
