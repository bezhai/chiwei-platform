"""Unified worker entry. Reads ``APP_NAME`` from env, boots :class:`Runtime`.

Goes through ``bootstrap.prepare_for_run`` so the boot phase order (load
graph -> register runtime-internal trigger wire -> migrate -> start
consumers / source loops) matches the FastAPI lifespan in ``app.main``
exactly. See ``app/runtime/bootstrap.py`` for the contract.
"""

from __future__ import annotations

import asyncio
import os

from inner_shared.logger import setup_logging

from app.data.bootstrap import ensure_business_schema
from app.runtime.bootstrap import prepare_for_run
from app.runtime.engine import Runtime


async def _main_async() -> None:
    """Async entry point."""
    # Phase 0: ensure business schema exists before any downstream
    # operation (dataflow nodes, RabbitMQ topology, sources, consumers).
    await ensure_business_schema()

    # Phase 1+2 (load graph) + Phase 1.5 (register trigger wire).
    # Worker entries don't pre-declare durable topology — start_consumers
    # already declares the consumer side, and workers are consumer
    # processes by definition.
    app_name = os.getenv("APP_NAME")
    assert app_name is not None  # main() validated this already
    await prepare_for_run(app_name)

    # Phase 3+ (migrate, start consumers, start source loops) are owned
    # by Runtime.run(). It already short-circuits register_runtime_trigger_wire
    # if the wire is already registered (the second call is a no-op).
    runtime = Runtime(app_name=app_name)
    await runtime.run()


def main() -> None:
    # APP_NAME picks the node subset this process serves. It MUST be
    # injected by PaaS per-Deployment — without it, ``Runtime`` would
    # fall back to ``DEFAULT_APP`` ("agent-service") and the worker
    # would silently come up as the wrong runtime (e.g. a vectorize-
    # worker pod running agent-service's node subset). Fail loud here.
    app_name = os.getenv("APP_NAME")
    if not app_name:
        raise RuntimeError(
            "runtime_entry requires the APP_NAME env to be set "
            "(per-Deployment injection is owned by PaaS)"
        )
    setup_logging(log_dir="/logs/agent-service", log_file=f"{app_name}.log")
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
