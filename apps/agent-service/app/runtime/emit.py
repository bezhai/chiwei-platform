"""emit(): publish a Data instance into the compiled dataflow graph.

Looks up every wire whose ``data_type`` matches the emitted instance and
dispatches to its consumers. In-process edges call the consumer directly
(awaiting completion); ``durable()`` edges hand off to the durable queue
layer; ``Sink.mq`` targets publish to their outbound queue.

In-process dispatch is strict: if any consumer raises, the remaining
fan-out (sibling consumers and later-matching wires) is aborted and the
exception propagates to ``emit``'s caller. Use ``.durable()`` when
independent isolation between consumers is required.
"""

from __future__ import annotations

import os

from app.runtime.data import Data
from app.runtime.graph import CompiledGraph, compile_graph
from app.runtime.placement import DEFAULT_APP, nodes_for_app

_graph: CompiledGraph | None = None


def reset_emit_runtime() -> None:
    global _graph
    _graph = None


def _get_graph() -> CompiledGraph:
    global _graph
    if _graph is None:
        _graph = compile_graph()
    return _graph


def _current_app() -> str:
    return os.getenv("APP_NAME") or DEFAULT_APP


async def emit(data: Data) -> None:
    graph = _get_graph()
    own_nodes = nodes_for_app(_current_app())
    cls = type(data)

    for w in graph.wires:
        if w.data_type is not cls:
            continue
        for c in w.consumers:
            if w.durable:
                # durable: publish to the consumer's queue; the bound
                # worker will consume and run it. No app-side filter.
                from app.runtime.durable import publish_durable

                await publish_durable(w, c, data)
                continue

            if c in own_nodes:
                # in-process: consumer is bound to (or falls through to)
                # THIS process's app — call directly.
                await c(**_inputs_for(c, data))
                continue

            # Consumer is in another process. A0 contract W4a: cross-app
            # dispatch without an explicit transport is banned because it
            # silently drops the Data on the floor. Surfaces the wiring bug
            # at the first emit instead of letting downstream logic
            # mysteriously never run.
            raise RuntimeError(
                f"wire({cls.__name__}).to({c.__name__}): cross-app dispatch "
                f"has no transport — add .durable() so emit publishes to "
                f"the consumer's queue. Current emit-side app is "
                f"{_current_app()!r}; consumer is bound elsewhere."
            )
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
