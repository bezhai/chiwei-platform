"""emit(): publish a Data instance into the compiled dataflow graph.

Looks up every wire whose ``data_type`` matches the emitted instance and,
for each wire, applies the optional ``when()`` predicate and dispatches
to the consumers. In-process edges call the consumer directly (awaiting
completion); ``durable()`` edges hand off to the durable queue layer.

In-process dispatch is strict: if any consumer raises, the remaining
fan-out (sibling consumers and later-matching wires) is aborted and the
exception propagates to ``emit``'s caller. Use ``.durable()`` when
independent isolation between consumers is required.

Persistence: a wire declared ``.as_latest()`` makes ``emit`` append a
new versioned row to the Data class's table before dispatching. This is
what lets a downstream ``with_latest(X)`` consumer (or any out-of-graph
``query()`` reader) actually find the row. The append happens once per
``emit`` even when the same Data class has multiple wires — what gets
persisted is the Data instance, not a per-wire copy.

``with_latest(X)`` inputs are resolved by fetching the latest ``X`` row
whose first Key field matches the same-named attribute on the emitted
data. ``select_latest`` returning ``None`` is treated as a wiring bug
and raised — a consumer that declared ``with_latest(X)`` cannot run
without an ``X`` to join against.
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

    # Persist before dispatch: any wire(cls).as_latest() declaration
    # means the Data must land in pg first, so downstream with_latest()
    # readers see it. Same instance, one append per emit — multiple
    # as_latest wires on the same class don't multiply the row count.
    if any(w.data_type is cls and w.as_latest for w in graph.wires):
        from app.runtime.persist import insert_append

        await insert_append(data)

    for w in graph.wires:
        if w.data_type is not cls:
            continue
        if w.predicate and not w.predicate(data):
            continue
        if w.debounce is not None:
            # debounce wire 走独立的 mq publish 路径（publish_debounce），
            # 不参与 in-process / sink / durable dispatch；compile_graph
            # 已保证 .debounce() 不能跟 .durable() / .as_latest() /
            # .when() / sink 等组合，这里直接 fan-out 到每个 consumer 后
            # continue。
            from app.runtime.debounce import publish_debounce

            for c in w.consumers:
                await publish_debounce(w, c, data)
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
                kwargs = await _resolve_inputs(c, data, w)
                await c(**kwargs)
                continue

            # Consumer is in another process. If the wire has Source.mq,
            # publish to that queue so the worker pod consumes it. Otherwise
            # silently skip — preserves prior behavior for wires that have
            # no MQ bridge declared.
            mq_src = next((s for s in w.sources if s.kind == "mq"), None)
            if mq_src is not None:
                await _mq_publish_for_source(mq_src, data)
        # Phase 2: sink dispatch — out-of-graph publish (RabbitMQ).
        # compile_graph 已校验 Sink.mq(name) ∈ ALL_ROUTES，这里直接调。
        for s in w.sinks:
            if s.kind == "mq":
                from app.runtime.sink_dispatch import _dispatch_mq_sink

                await _dispatch_mq_sink(s, data)


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
            latest = await select_latest(t, {key: val})
            if latest is None:
                raise RuntimeError(
                    f"with_latest({t.__name__}) found no row for "
                    f"{key}={val!r}; an upstream "
                    f"wire({t.__name__}).as_latest() must have written at "
                    f"least one version before {consumer.__name__} fires"
                )
            kwargs[name] = latest
    return kwargs


async def _mq_publish_for_source(src, data: Data) -> None:
    """Publish ``data`` to the mq queue declared by ``src``.

    Looks up the Route in ALL_ROUTES by queue name (queue must be a known
    route — compile_graph rejects unknown queues at startup). Body is
    ``data.model_dump(mode='json')`` to mirror Source.mq consumer-side
    decoding. Lane resolution happens inside ``mq.publish``.
    """
    from app.infra.rabbitmq import mq
    from app.runtime.sink_dispatch import _route_by_queue

    queue_name = src.params["queue"]
    route = _route_by_queue(queue_name)
    if route is None:
        raise RuntimeError(
            f"emit() cannot publish {type(data).__name__} to "
            f"Source.mq({queue_name!r}): queue is not in ALL_ROUTES. "
            f"Add it to app/infra/rabbitmq.py."
        )
    body = data.model_dump(mode="json")
    await mq.publish(route, body)
