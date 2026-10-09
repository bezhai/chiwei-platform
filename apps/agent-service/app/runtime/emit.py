"""emit(): publish a Data instance into the compiled dataflow graph.

Looks up every wire whose ``data_type`` matches the emitted instance and
dispatches to its consumers. In-process edges call the consumer directly
(awaiting completion); ``durable()`` edges hand off to the durable queue
layer; ``Sink.mq`` targets publish to their outbound queue.

The registry holds only this process's wires (the plugin host registers its
app's edges), so a consumer that is not durable runs here.

In-process dispatch is strict: if any consumer raises, the remaining
fan-out (sibling consumers and later-matching wires) is aborted and the
exception propagates to ``emit``'s caller. Use ``.durable()`` when
independent isolation between consumers is required.
"""

from __future__ import annotations

from app.runtime.data import Data
from app.runtime.graph import CompiledGraph, compile_graph

_graph: CompiledGraph | None = None


def reset_emit_runtime() -> None:
    global _graph
    _graph = None


def _get_graph() -> CompiledGraph:
    global _graph
    if _graph is None:
        _graph = compile_graph()
    return _graph


async def emit(data: Data) -> None:
    graph = _get_graph()
    cls = type(data)

    for w in graph.wires:
        if w.data_type is not cls:
            continue
        for c in w.consumers:
            if w.durable:
                # durable: publish to the consumer's queue; this process's
                # durable consumer picks it up and runs it.
                from app.runtime.durable import publish_durable

                await publish_durable(w, c, data)
            else:
                await c(**_inputs_for(c, data))
        # Phase 2: sink dispatch — out-of-graph publish (RabbitMQ).
        # compile_graph 已校验 Sink.mq(name) ∈ ALL_ROUTES，这里直接调。
        for s in w.sinks:
            if s.kind == "mq":
                from app.runtime.sink_dispatch import _dispatch_mq_sink

                await _dispatch_mq_sink(s, data)


def _inputs_for(consumer, data: Data) -> dict:
    """The consumer's keyword arguments: ``data`` for each parameter of its type."""
    from app.runtime.node import inputs_of

    return {name: data for name, t in inputs_of(consumer).items() if t is type(data)}
