import asyncio
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from inner_shared import hello as shared_hello
from inner_shared.logger import setup_logging
from inner_shared.middlewares.context_propagation import (
    create_context_propagation_middleware,
)

from app.api.middleware import HeaderContextMiddleware, PrometheusMiddleware
from app.api.routes import router as api_router
from app.infra.config import settings
from app.infra.qdrant import init_collections

load_dotenv()
setup_logging(log_dir="/logs/agent-service", log_file="app.log")

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle — init resources, start consumers, teardown."""
    await init_collections()
    logger.info("shared pkg loaded: %s", shared_hello())

    # Wire up the dataflow graph before anything in this process emit()s.
    # proactive.py emits Message directly via runtime emit, which dispatches
    # via WIRING_REGISTRY — without this load step the registry would be
    # empty here and the call would silently no-op. The FastAPI main
    # process is a *producer* (proactive Message -> vectorize-worker), so
    # it must also pre-declare the durable routes; otherwise messages
    # publish before the consumer pod has had a chance to declare its
    # queue, and the broker drops them.
    from app.runtime.bootstrap import declare_durable_topology, load_dataflow_graph

    load_dataflow_graph()

    # Phase 4: migrate schema BEFORE start_consumers — durable consumer
    # for GlimpseRequest needs data_glimpse_request table to exist.
    from app.runtime.engine import Runtime

    runtime_for_sources = Runtime(
        app_name="agent-service",
        migrate_schema_on_run=False,  # we drive migrate explicitly below
    )
    await runtime_for_sources.migrate_schema()

    # Register the runtime-internal delayed-trigger wire BEFORE
    # start_consumers (which calls compile_graph and freezes the
    # WIRING_REGISTRY snapshot). Runtime.run() does the same; this
    # branch covers the FastAPI lifespan path that drives migrate /
    # consumers / source loops directly without going through run().
    from app.infra.rabbitmq import KNOWN_APPS_FOR_DELAYED_TRIGGER
    from app.runtime.delayed_trigger import register_runtime_trigger_wire

    if "agent-service" in KNOWN_APPS_FOR_DELAYED_TRIGGER:
        register_runtime_trigger_wire("agent-service")

    if settings.rabbitmq_url:
        await declare_durable_topology()

    # Load skill definitions
    import os
    from pathlib import Path

    from app.skills.registry import SkillRegistry, skill_reload_loop

    skills_dir = Path(
        os.environ.get(
            "SKILLS_DIR", str(Path(__file__).parent / "skills" / "definitions")
        )
    )
    SkillRegistry.load_all(skills_dir)

    # Start hot-reload loop
    reload_task = asyncio.create_task(skill_reload_loop(skills_dir))

    # Start MQ consumers (only when RabbitMQ is configured)
    if settings.rabbitmq_url:
        # Phase 2: post-safety 改走 runtime durable consumer。旧
        # start_post_consumer 删除（替代为 wire(PostSafetyRequest)
        # .to(run_post_safety).durable()）；runtime 自动按 placement.bind
        # 过滤启动属于本 app 的 consumer。
        # Phase 5a: chat_request 也改走 runtime durable consumer（chat_node），
        # 旧 chat_consumer / pipeline.stream_chat 已删。
        from app.runtime.debounce import start_debounce_consumers
        from app.runtime.durable import start_consumers

        await start_consumers(app_name="agent-service")
        logger.info("Runtime durable consumers started for agent-service")
        await start_debounce_consumers(app_name="agent-service")
        logger.info("Runtime debounce consumers started for agent-service")

    from app.runtime.http_source import register_http_sources

    register_http_sources(app)
    logger.info("dataflow http sources registered")

    # Phase 4: start cron / interval / mq source loops + watchdog.
    # Must run AFTER register_http_sources so HTTP routes are in place.
    await runtime_for_sources.start_source_loops()
    logger.info("dataflow source loops started")

    yield

    # Phase 4: stop source loops first; in-progress sources can still
    # emit() to durable consumers cleanly because consumers are still alive.
    logger.info("dataflow source loops stopping")
    await runtime_for_sources.stop_source_loops()

    # Phase 2: stop runtime durable consumers cleanly before tearing
    # down RabbitMQ connection (otherwise late deliveries race with close).
    if settings.rabbitmq_url:
        from app.runtime.debounce import stop_debounce_consumers
        from app.runtime.durable import stop_consumers

        await stop_debounce_consumers()
        await stop_consumers()

    # Cancel skill reload task
    reload_task.cancel()
    try:
        await reload_task
    except asyncio.CancelledError:
        pass

    # Close RabbitMQ connection
    if settings.rabbitmq_url:
        from app.infra.rabbitmq import mq

        await mq.close()


app = FastAPI(lifespan=lifespan)

# Prometheus metrics middleware (outermost — records all requests)
app.add_middleware(PrometheusMiddleware)

# Header context middleware (trace_id, app_name, lane)
app.add_middleware(HeaderContextMiddleware)

# x-ctx-* context propagation (for sidecar lane routing)
app.add_middleware(create_context_propagation_middleware())

# Register routes
app.include_router(api_router)
