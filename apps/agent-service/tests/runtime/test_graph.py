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


def test_http_source_consumer_must_be_in_default_app(monkeypatch):
    # register_http_sources() mounts FastAPI routes on the process that
    # loaded the wire. A consumer bound to another app would see the
    # request return 202 to the caller while emit() filters it out by
    # APP_NAME — silent drop. compile_graph must reject this at boot.
    from app.runtime.source import Source

    monkeypatch.delenv("APP_NAME", raising=False)

    @node
    async def worker_only(m: M) -> None: ...

    bind(worker_only).to_app("vectorize-worker")
    wire(M).to(worker_only).from_(Source.http("/api/trigger"))

    with pytest.raises(GraphError, match="HTTP sources are mounted only"):
        compile_graph()


def test_http_source_consumer_of_the_app_this_process_runs_is_fine(monkeypatch):
    """world 的进程也是 FastAPI 主进程：它自己的 HTTP 路由挂在它自己那里。"""
    from app.runtime.source import Source

    monkeypatch.setenv("APP_NAME", "world")

    @node
    async def world_route(m: M) -> None: ...

    bind(world_route).to_app("world")
    wire(M).to(world_route).from_(Source.http("/admin/world/x"))

    compile_graph()  # no raise


def test_http_source_consumer_of_another_app_is_refused_in_this_process(monkeypatch):
    from app.runtime.source import Source

    monkeypatch.setenv("APP_NAME", "agent-service")

    @node
    async def world_route(m: M) -> None: ...

    bind(world_route).to_app("world")
    wire(M).to(world_route).from_(Source.http("/admin/world/x"))

    with pytest.raises(GraphError, match="HTTP sources are mounted only"):
        compile_graph()


def test_the_app_to_check_against_can_be_named_explicitly(monkeypatch):
    """启动时按要加载的那个 App 判，不依赖环境变量先设好。"""
    from app.runtime.source import Source

    monkeypatch.delenv("APP_NAME", raising=False)

    @node
    async def world_route(m: M) -> None: ...

    bind(world_route).to_app("world")
    wire(M).to(world_route).from_(Source.http("/admin/world/x"))

    compile_graph(app_name="world")  # no raise
    with pytest.raises(GraphError, match="HTTP sources are mounted only"):
        compile_graph(app_name="agent-service")


def test_http_source_consumer_in_default_app_ok(monkeypatch):
    # Default-app (unbound) consumer is fine.
    from app.runtime.source import Source

    monkeypatch.delenv("APP_NAME", raising=False)

    @node
    async def main_handler(m: M) -> None: ...

    wire(M).to(main_handler).from_(Source.http("/api/trigger"))
    compile_graph()  # no raise


# ---------------------------------------------------------------------------
# A0 contract —缺失断言 W4a
# ---------------------------------------------------------------------------


def test_w4a_cross_app_compile_does_not_reject():
    # W4a 是 runtime 检查（在 emit() 触发时 raise），不是 compile-time。
    # compile_graph 无法判断 emit 触发方所在 app（无状态），所以这里只确认
    # compile 通过，真正的 raise 测试见 tests/runtime/test_emit_cross_process.py
    # 的 test_emit_raises_when_consumer_in_other_app_without_durable。
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
