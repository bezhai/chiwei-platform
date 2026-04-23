"""compile_graph(): startup validation for the wired dataflow graph.

Walks ``WIRING_REGISTRY`` and verifies that:
  * every consumer referenced by a wire is decorated with ``@node``;
  * every ``.with_latest(X)`` has a matching ``wire(X).as_latest()`` declared
    somewhere else in the graph;
  * every consumer's signature actually accepts the data types the wire
    routes to it (the primary ``data_type`` plus any ``with_latest`` types).

Returns a ``CompiledGraph`` summarising the data types, nodes, and wires
seen. Errors surface as ``GraphError`` at startup so mis-wired graphs
never reach traffic.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.runtime.data import Data
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
    wires = list(WIRING_REGISTRY)

    # 1) every consumer in wires must be @node-registered
    for w in wires:
        for c in w.consumers:
            if c not in NODE_REGISTRY:
                raise GraphError(
                    f"wire({w.data_type.__name__}).to({c.__name__}): consumer "
                    f"not registered as @node"
                )

    # 2) .with_latest(X) requires some wire(X).as_latest() to exist
    latest_types = {w.data_type for w in wires if w.as_latest}
    for w in wires:
        for t in w.with_latest:
            if t not in latest_types:
                raise GraphError(
                    f"wire({w.data_type.__name__}).with_latest({t.__name__}) "
                    f"requires wire({t.__name__}).as_latest() declared somewhere"
                )

    # 3) consumer signature compatibility
    for w in wires:
        for c in w.consumers:
            ins = inputs_of(c)
            param_types = set(ins.values())
            needed = {w.data_type, *w.with_latest}
            if not needed.issubset(param_types):
                raise GraphError(
                    f"wire({w.data_type.__name__}).to({c.__name__}): consumer "
                    f"signature {ins} does not accept {needed}"
                )

    # 4) placement consistency: a wire's consumers must all be bound to
    # the same app (or all unbound). Mixed-app wires break
    # ``start_consumers(app_name)``'s all-or-nothing filter, so refuse
    # the graph instead of silently dropping messages at runtime.
    # Only enforce when placement is actually in use — tests that never
    # call ``bind()`` must remain unaffected.
    from app.runtime.placement import iter_bindings

    bindings = dict(iter_bindings())
    if bindings:
        for w in wires:
            apps = {bindings.get(c) for c in w.consumers}
            if len(apps) > 1:
                labels = sorted(
                    f"{c.__name__}->{bindings.get(c, '<unbound>')}"
                    for c in w.consumers
                )
                raise GraphError(
                    f"wire({w.data_type.__name__}): consumers span mixed apps "
                    f"({', '.join(labels)}); split the wire or rebind "
                    f"consumers so they share one app"
                )

    data_types: set[type[Data]] = {w.data_type for w in wires} | {
        t for w in wires for t in w.with_latest
    }
    nodes = {c for w in wires for c in w.consumers}
    return CompiledGraph(data_types=data_types, nodes=nodes, wires=wires)
