"""Boot the dataflow graph: register, validate, and pre-declare topology.

Three helpers, all meant to be called from any process that participates
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

  * :func:`prepare_for_run` is the unified startup helper both
    entrypoints (FastAPI ``app.main`` lifespan and worker
    ``app.workers.runtime_entry``) call so they share the same boot
    contract. It composes the phases that previously lived open-coded
    in two places:

      1. ``load_dataflow_graph()`` — Phase 1+2: register wiring +
         deployment side-effects, then compile_graph to validate.
      2. ``register_runtime_trigger_wire(app)`` — Phase 1.5: append the
         runtime-internal delayed-trigger wire into ``WIRING_REGISTRY``
         BEFORE downstream consumers freeze a snapshot. Skips silently
         for apps outside ``KNOWN_APPS_FOR_DELAYED_TRIGGER`` (same
         policy as ``Runtime.run``).
      3. ``declare_durable_topology()`` — Phase 3.5 (opt-in via
         ``declare_topology=True``): producer processes that may emit
         BEFORE any consumer pod has had time to declare its queue
         must pre-declare the routes themselves. Worker entries
         already declare their own routes via ``start_consumers``, so
         they leave this off.

    What it does **not** do: ``ensure_business_schema``,
    ``migrate_schema``, ``start_consumers``,
    ``start_source_loops``. Those depend on resources / Runtime state
    that the entrypoint owns. ``prepare_for_run`` is only the part
    that's identical in both entries — keeping the rest at the call
    site makes the per-entry phase order explicit.
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


async def prepare_for_run(
    app_name: str,
    *,
    declare_topology: bool = False,
) -> None:
    """Unified startup helper for FastAPI + worker entries. See module docstring.

    Phase order is config-wiring -> load-graph -> register-trigger-wire ->
    (opt) declare-durable-topology. Reversing the graph phases would either
    let compile_graph snapshot a registry without the trigger wire (durable
    consumers then drop trigger envelopes) or let a producer publish
    before its consumer's queue exists (broker silently drops).
    """
    # Phase 0: process-level config wiring. Dynamic Config resolves
    # per-lane; both entries (FastAPI lifespan / worker runtime_entry) go
    # through here, so the provider is set no matter which process boots —
    # wiring it in only one entry would leave the other reading prod
    # config on coe/ppe lanes (the classic dual-entry footgun).
    from inner_shared.dynamic_config import dynamic_config

    from app.runtime.lane_policy import current_deployment_lane

    dynamic_config.set_lane_provider(current_deployment_lane)

    # Phase 1+2: register all @node / wire() / bind() side-effects, then
    # compile_graph to validate the topology.
    load_dataflow_graph()

    # Phase 1.5: append the runtime-internal trigger wire BEFORE any
    # consumer freezes a WIRING_REGISTRY snapshot. Apps that don't have
    # a trigger route configured fall through silently — emit_delayed
    # with durability='durable' will then fail-fast at call time.
    from app.infra.rabbitmq import KNOWN_APPS_FOR_DELAYED_TRIGGER
    from app.runtime.delayed_trigger import register_runtime_trigger_wire

    if app_name in KNOWN_APPS_FOR_DELAYED_TRIGGER:
        register_runtime_trigger_wire(app_name)
    else:
        logger.info(
            "bootstrap: app=%s has no delayed trigger route; "
            "emit_delayed(durability='durable') will be unavailable",
            app_name,
        )

    # Phase 3.5: opt-in pre-declare of every durable wire's route. Only
    # producer-side entries (e.g. FastAPI lifespan publishing into a
    # downstream worker's queue) need this; worker entries already
    # declare their own routes when start_consumers runs.
    if declare_topology:
        await declare_durable_topology()
