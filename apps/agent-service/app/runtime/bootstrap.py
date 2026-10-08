"""Boot the dataflow graph: register, validate, and pre-declare topology.

Three helpers, all meant to be called from any process that participates
in the dataflow runtime:

  * :func:`load_dataflow_graph` imports the wiring modules of ONE app —
    the ones ``app.deployment.APP_WIRING`` lists for it — which populate
    ``WIRING_REGISTRY`` (and, through their own imports, the Data / node /
    inbox registries), then calls ``compile_graph`` to validate the result.
    Another app's wiring is never imported, so its code never enters this
    process. Without this step a process that ``emit()``s sees an empty
    registry and silently no-ops — the exact bug the FastAPI main process
    had before this module existed.

  * :func:`declare_durable_topology` declares the RabbitMQ queue +
    binding for every ``.durable()`` wire's ``(data, consumer)`` route
    on broker startup. ``durable.publish_durable`` writes to those
    routes; ``start_consumers`` only declares them on the consumer side.
    A producer that boots before the consumer pod would otherwise
    publish to a route that doesn't exist yet, and the broker silently
    drops the message. Calling this helper from every potential
    publisher closes that window. Re-declaring is a no-op on the
    broker, so it is safe to call even though ``start_consumers``
    declares the consumer's own routes as well.

  * :func:`prepare_for_run` is the startup helper ``app.main``'s lifespan
    calls. It composes:

      1. ``load_dataflow_graph(app_name)`` — import that app's wiring,
         then compile_graph to validate.
      2. ``declare_durable_topology()`` (opt-in via
         ``declare_topology=True``): a process that may emit BEFORE any
         consumer has had time to declare its queue must pre-declare
         the routes itself.

    What it does **not** do: ``ensure_business_schema``,
    ``migrate_schema``, ``start_consumers``,
    ``start_source_loops``. Those depend on resources / Runtime state
    that the lifespan owns; keeping them at the call site makes the
    phase order explicit.
"""

from __future__ import annotations

import importlib
import logging

from app.runtime.graph import CompiledGraph, compile_graph

logger = logging.getLogger(__name__)


def load_dataflow_graph(app_name: str) -> CompiledGraph:
    """Import ``app_name``'s wiring modules (and nothing else), then compile + validate.

    An app that ``app.deployment.APP_WIRING`` doesn't declare fails here,
    before anything starts — a typo'd or unset ``APP_NAME`` must not boot a
    process that looks healthy while serving nothing.
    """
    from app.deployment import APP_WIRING

    modules = APP_WIRING.get(app_name)
    if modules is None:
        raise RuntimeError(
            f"app {app_name!r} is not declared in app.deployment.APP_WIRING "
            f"(declared: {sorted(APP_WIRING)})"
        )
    for module in modules:
        importlib.import_module(module)

    graph = compile_graph(app_name)
    logger.info(
        "dataflow graph loaded for app=%s: %d wires, %d nodes, %d data types",
        app_name,
        len(graph.wires),
        len(graph.nodes),
        len(graph.data_types),
    )
    return graph


async def declare_durable_topology() -> None:
    """Idempotently prepare the exchange, then declare every durable wire's route.

    Two separate things, and they used to be fused: an empty route list
    returned early, so the exchange was never declared either.

    They are not the same question. Callers only get here through
    ``prepare_for_run(declare_topology=True)``, and that flag already says
    "this process is going to publish". Whether some durable *consumer*
    happens to be registered says nothing about that. Fusing them meant a
    process with outbound-only MQ — every ``Source.mq`` gated off, nothing
    durable left in the registry — got no exchange, and every publish died
    on ``RuntimeError: must call declare_topology() first``.

    That is exactly what the living-engine experiment lane looks like: four
    interval sources, zero MQ consumers, and it still speaks to the channel
    worker through ``ChatResponseSegment``. It could hear and not speak.

    Processes that genuinely have no use for MQ are filtered one level up,
    by not passing the flag at all (``main.py`` gates it on
    ``settings.rabbitmq_url``).
    """
    from app.infra.rabbitmq import mq
    from app.runtime.durable import _route_for
    from app.runtime.wire import WIRING_REGISTRY

    await mq.connect()
    await mq.declare_topology()

    routes = [
        _route_for(w, c)
        for w in WIRING_REGISTRY
        if w.durable
        for c in w.consumers
    ]
    for route in routes:
        await mq.declare_route(route)
    logger.info("durable topology declared: %d route(s)", len(routes))


async def prepare_for_run(
    app_name: str,
    *,
    declare_topology: bool = False,
) -> None:
    """Startup helper for ``app.main``'s lifespan. See module docstring.

    Phase order is config-wiring -> load-graph -> (opt)
    declare-durable-topology: declaring before the graph is loaded would
    miss the durable wires the graph registers, and a producer would
    publish before its consumer's queue exists (broker silently drops).
    """
    # Phase 0: process-level config wiring. Dynamic Config resolves
    # per-lane, so the provider is set before anything reads config —
    # otherwise coe/ppe lanes would read prod config.
    from inner_shared.dynamic_config import dynamic_config

    from app.runtime.lane_policy import current_deployment_lane

    dynamic_config.set_lane_provider(current_deployment_lane)

    # Phase 1+2: import this app's wiring (its @node / wire() / bind() /
    # inbox() side-effects, nothing of any other app), then compile_graph to
    # validate the topology.
    load_dataflow_graph(app_name)

    # Phase 3.5: opt-in pre-declare of every durable wire's route, for a
    # process that publishes (main.py gates it on a configured broker).
    if declare_topology:
        await declare_durable_topology()
