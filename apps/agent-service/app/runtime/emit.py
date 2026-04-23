"""emit(): publish a Data instance into the compiled dataflow graph.

Looks up every wire whose ``data_type`` matches the emitted instance and,
for each wire, applies the optional ``when()`` predicate and dispatches
to the consumers. In-process edges call the consumer directly (awaiting
completion); ``durable()`` edges hand off to the durable queue layer
(filled in by Task 0.11).

In-process dispatch is strict: if any consumer raises, the remaining
fan-out (sibling consumers and later-matching wires) is aborted and the
exception propagates to ``emit``'s caller. Use ``.durable()`` when
independent isolation between consumers is required.

``with_latest(X)`` inputs are resolved by fetching the latest ``X`` row
whose first Key field matches the same-named attribute on the emitted
data. Phase 0 MVP: a single-column key join by name; richer resolution
will come if/when real wiring needs it.
"""

from __future__ import annotations

from app.runtime.data import Data
from app.runtime.graph import CompiledGraph, compile_graph

_graph: CompiledGraph | None = None


def reset_emit_runtime() -> None:
    global _graph
    _graph = None


def _get_graph() -> CompiledGraph:
    # T0.14: filter wires by the current deployment's nodes_for_app()
    # once Deployment.bind() lands. Today every process sees every wire.
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
        if w.predicate and not w.predicate(data):
            continue
        for c in w.consumers:
            if w.durable:
                # handled by Task 0.11 — durable queue
                from app.runtime.durable import publish_durable

                await publish_durable(w, c, data)
            else:
                kwargs = await _resolve_inputs(c, data, w)
                await c(**kwargs)


async def _resolve_inputs(consumer, data: Data, wire_spec) -> dict:
    from app.runtime.data import key_fields
    from app.runtime.node import inputs_of
    from app.runtime.persist import select_latest

    ins = inputs_of(consumer)
    kwargs: dict = {}
    for name, t in ins.items():
        if t is type(data):
            kwargs[name] = data
        elif t in wire_spec.with_latest:
            key = key_fields(t)[0]
            val = getattr(data, key, None)
            if val is None:
                raise RuntimeError(
                    f"with_latest({t.__name__}) needs {key} on "
                    f"{type(data).__name__}"
                )
            kwargs[name] = await select_latest(t, {key: val})
    return kwargs
