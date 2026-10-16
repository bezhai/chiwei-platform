"""compile_graph(): startup validation for the wired dataflow graph.

Walks ``WIRING_REGISTRY`` and verifies that:
  * every consumer referenced by a wire is decorated with ``@node``;
  * every consumer's signature accepts exactly the data type the wire
    routes to it;
  * every ``Sink.mq`` names a known route.

Returns a ``CompiledGraph`` summarising the data types, nodes, and wires
seen. Errors surface as ``GraphError`` at startup so mis-wired graphs
never reach traffic.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.data.schema import Data
from app.runtime.node import NODE_REGISTRY, inputs_of
from app.runtime.wire import WIRING_REGISTRY, WireSpec


class GraphError(Exception):
    pass


@dataclass
class CompiledGraph:
    data_types: set[type[Data]]
    nodes: set
    wires: list[WireSpec]


def compile_graph() -> CompiledGraph:
    """Validate the wired graph of this process (the wires its plugins registered)."""
    wires = list(WIRING_REGISTRY)

    # 1) every consumer in wires must be @node-registered
    for w in wires:
        for c in w.consumers:
            if c not in NODE_REGISTRY:
                raise GraphError(
                    f"wire({w.data_type.__name__}).to({c.__name__}): consumer "
                    f"not registered as @node"
                )

    # 3) consumer signature must equal the wire's declared inputs
    # exactly. Subset-only matching ("consumer accepts at least these")
    # lets a consumer declare an extra Data param that no wire ever
    # populates — startup looks fine, then emit() raises a missing-kwarg
    # at first traffic. Strict equality also encodes the framework's
    # 1-consumer-1-wire design: if a function needs to react to two
    # different data types, write two @nodes (or one with ``Union[A, B]``
    # once that's modeled), don't reuse the same callable across wires.
    for w in wires:
        for c in w.consumers:
            ins = inputs_of(c)
            param_types = set(ins.values())
            needed = {w.data_type}
            if param_types != needed:
                extra = param_types - needed
                missing = needed - param_types
                hint = []
                if missing:
                    hint.append(
                        f"missing {sorted(t.__name__ for t in missing)}"
                    )
                if extra:
                    hint.append(
                        f"extra {sorted(t.__name__ for t in extra)} "
                        f"(split into a separate @node — a consumer must "
                        f"appear on exactly one wire)"
                    )
                raise GraphError(
                    f"wire({w.data_type.__name__}).to({c.__name__}): "
                    f"consumer signature {ins} does not match the wire "
                    f"inputs {sorted(t.__name__ for t in needed)} "
                    f"({'; '.join(hint)})"
                )

    # 5b) Phase 2 sink dispatch validation: every Sink.mq(name) must
    # reference a queue declared in ALL_ROUTES, otherwise sink dispatch
    # wouldn't know which routing key to use when publishing (lane
    # fan-out + queue->rk binding live there). Catching this at compile
    # time means a typo surfaces at boot, not at the first emit.
    from app.infra.rabbitmq import ALL_ROUTES, CHANNEL_PARTITIONED_ROUTES
    known_queues = {r.queue for r in ALL_ROUTES}
    sink_errors: list[str] = []
    for w in wires:
        for s in w.sinks:
            if s.kind == "mq":
                q = s.params["queue"]
                if q not in known_queues:
                    sink_errors.append(
                        f"wire({w.data_type.__name__}).to(Sink.mq({q!r})): "
                        f"queue not in ALL_ROUTES; sink dispatch needs a "
                        f"registered route to know the routing key. "
                        f"Add Route({q!r}, ...) to ALL_ROUTES first."
                    )
                elif (
                    q in CHANNEL_PARTITIONED_ROUTES
                    and "channel" not in w.data_type.model_fields
                ):
                    # 这几条队列按 channel 分区（出站 owner 按渠道拆开），rk 只能
                    # 由消息自己的 channel 决定。Data 说不出 channel 的话，错误
                    # 要等到第一条真实消息 emit 时才在 dispatch 里炸。
                    sink_errors.append(
                        f"wire({w.data_type.__name__}).to(Sink.mq({q!r})): "
                        f"{q} is channel-partitioned but "
                        f"{w.data_type.__name__} has no 'channel' field, so "
                        f"dispatch cannot pick a routing key. Add "
                        f"'channel: str' to the Data."
                    )
    if sink_errors:
        raise GraphError(
            "sink dispatch validation failed:\n  - " + "\n  - ".join(sink_errors)
        )

    data_types: set[type[Data]] = {w.data_type for w in wires}
    nodes = {c for w in wires for c in w.consumers}
    return CompiledGraph(data_types=data_types, nodes=nodes, wires=wires)
