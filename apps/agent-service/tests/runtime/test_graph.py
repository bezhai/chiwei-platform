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


class JoinKeyOnly(Data):
    """W2a: with_latest target whose Key (sid) is absent on M.mid — must reject."""

    sid: Annotated[str, Key]
    v: int = 0


class MatchingKey(Data):
    """W2a: with_latest target whose Key (mid) matches M.mid — must pass."""

    mid: Annotated[str, Key]
    v: int = 0


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


# ---------------------------------------------------------------------------
# A0 contract —缺失断言 W2a / W4a / W14
# ---------------------------------------------------------------------------


def test_w2a_with_latest_join_key_must_exist_on_primary():
    # W2a: with_latest(X) 把 X 的第一个 Key 当作 join key 在 emit 时从 primary
    # data 同名属性上取（emit.py:_resolve_inputs 的 getattr(data, key)）。primary
    # 类没有同名属性时，当前是 emit 首次触发才 raise RuntimeError——compile_graph
    # 必须在 boot 时就拒绝。
    # M 只有 mid，JoinKeyOnly.Key=sid → primary 没有 sid 属性
    @node
    async def needs_join(m: M, s: JoinKeyOnly) -> None: ...

    @node
    async def join_producer(s: JoinKeyOnly) -> None: ...

    wire(JoinKeyOnly).to(join_producer).as_latest()
    wire(M).to(needs_join).with_latest(JoinKeyOnly)

    with pytest.raises(GraphError, match="with_latest.*JoinKeyOnly.*sid"):
        compile_graph()


def test_w2a_with_latest_join_key_present_ok():
    # primary 类拥有 join key 同名属性时通过
    @node
    async def reader(m: M, mk: MatchingKey) -> None: ...

    @node
    async def producer(mk: MatchingKey) -> None: ...

    wire(MatchingKey).to(producer).as_latest()
    wire(M).to(reader).with_latest(MatchingKey)
    compile_graph()  # no raise


def test_w4a_cross_app_compile_does_not_reject():
    # W4a 是 runtime 检查（在 emit() 触发时 raise），不是 compile-time。
    # compile_graph 无法判断 emit 触发方所在 app（无状态），所以这里只确认
    # compile 通过，真正的 raise 测试见 tests/runtime/test_emit_cross_process.py
    # 的 test_emit_raises_when_no_mq_source_and_consumer_other_app。
    @node
    async def vectorize_consumer(x: X) -> None: ...

    bind(vectorize_consumer).to_app("vectorize-worker")
    wire(X).to(vectorize_consumer)
    # compile 通过——emit 触发时才知道是 cross-app 静默 skip
    compile_graph()


def test_w4a_cross_app_wire_with_durable_ok():
    # 跨 app + .durable() 走 publish_durable 队列，路径正确
    @node
    async def vectorize_consumer(x: X) -> None: ...

    bind(vectorize_consumer).to_app("vectorize-worker")
    wire(X).to(vectorize_consumer).durable()
    compile_graph()  # no raise


def test_w4a_same_app_wire_without_transport_ok():
    # 同 app（in-process）不需要 transport，正常通过
    @node
    async def local_consumer(x: X) -> None: ...

    wire(X).to(local_consumer)
    compile_graph()  # no raise


def test_w14_on_error_non_default_requires_durable():
    # W14: on_error != 'dlq' 时 wire 必须 .durable()。in-process 边异常直接
    # propagate，on_error 在 in-process 路径上没有意义；声明了 != 'dlq' 但忘
    # .durable() 说明业务认知错了，必须 boot 时拒绝。
    @node
    async def reviewer(x: X) -> None: ...

    wire(X).to(reviewer).on_error("manual-review")  # 缺 .durable()

    with pytest.raises(GraphError, match="on_error.*durable|durable.*on_error"):
        compile_graph()


def test_w14_on_error_dlq_default_ok_without_durable():
    # 默认 on_error='dlq' 的 in-process wire 是合法状态——dlq 是默认值，
    # 业务没显式声明任何 policy，不算违反 W14
    @node
    async def consumer(x: X) -> None: ...

    wire(X).to(consumer)  # 没 .on_error()，没 .durable()
    compile_graph()  # no raise


def test_w14_on_error_with_durable_ok():
    # on_error != 'dlq' + .durable() 是设计内的正确组合
    @node
    async def reviewer(x: X) -> None: ...

    wire(X).to(reviewer).durable().on_error("manual-review")
    compile_graph()  # no raise
