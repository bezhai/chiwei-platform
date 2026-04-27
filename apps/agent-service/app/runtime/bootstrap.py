"""Boot the dataflow graph: register, validate, and pre-declare topology.

Two helpers, both meant to be called from any process that participates
in the dataflow runtime:

  * :func:`load_dataflow_graph` runs the side-effect imports of
    ``app.deployment`` and ``app.wiring`` (which populate the placement
    binding map and ``WIRING_REGISTRY``), then calls ``compile_graph``
    to validate the result. Without this step a process that ``emit()``s
    sees an empty registry and silently no-ops — the exact bug the
    FastAPI main process had before this module existed.

  * :func:`declare_durable_topology` declares the RabbitMQ queue +
    binding for every ``.durable()`` wire's ``(data, consumer)`` route
    on broker startup. ``durable.publish_durable`` writes to those
    routes; ``start_consumers`` only declares them on the consumer side.
    A producer that boots before the consumer pod would otherwise
    publish to a route that doesn't exist yet, and the broker silently
    drops the message. Calling this helper from every potential
    publisher closes that window. Re-declaring is a no-op on the
    broker, so it is safe to call from ``runtime_entry`` as well even
    though ``Runtime`` already declares the consumer's own routes.
"""

from __future__ import annotations

import logging

from app.runtime.graph import CompiledGraph, compile_graph

logger = logging.getLogger(__name__)


def load_dataflow_graph() -> CompiledGraph:
    """Side-effect import wiring/deployment, then compile + validate."""
    import app.deployment  # noqa: F401 — registers @node -> app bindings
    import app.wiring  # noqa: F401 — registers wire() declarations

    graph = compile_graph()
    logger.info(
        "dataflow graph loaded: %d wires, %d nodes, %d data types",
        len(graph.wires),
        len(graph.nodes),
        len(graph.data_types),
    )
    return graph


async def declare_durable_topology() -> None:
    """Idempotently declare every durable wire's route on the broker.

    Skips entirely when no durable wire exists (avoids forcing an MQ
    connection on processes that don't need one).
    """
    from app.runtime.durable import _route_for
    from app.runtime.wire import WIRING_REGISTRY

    routes = [
        _route_for(w, c)
        for w in WIRING_REGISTRY
        if w.durable
        for c in w.consumers
    ]
    if not routes:
        return

    from app.infra.rabbitmq import mq

    await mq.connect()
    await mq.declare_topology()
    for route in routes:
        await mq.declare_route(route)
    logger.info("durable topology declared: %d route(s)", len(routes))
