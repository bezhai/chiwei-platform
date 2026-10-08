import asyncio
import logging
import os
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
from app.data.bootstrap import ensure_business_schema
from app.infra.config import settings
from app.runtime.placement import DEFAULT_APP

load_dotenv()
setup_logging(log_dir="/logs/agent-service", log_file="app.log")

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle — init resources, start consumers, teardown.

    The app this process serves is ``APP_NAME`` (injected by PaaS per
    Deployment); only that app's wiring is loaded and only its consumers,
    sources and inboxes start.
    """
    app_name = os.getenv("APP_NAME") or DEFAULT_APP

    # Phase 2: ensure business schema exists before any downstream operation
    # (RabbitMQ topology, sources, consumers)
    await ensure_business_schema()

    logger.info("shared pkg loaded: %s", shared_hello())

    # Wire up the dataflow graph + (when MQ is configured) pre-declare
    # durable topology so this producer-side process can emit() to
    # downstream queues before their consumers have declared them.
    # See app/runtime/bootstrap.py for the contract.
    from app.runtime.bootstrap import prepare_for_run

    await prepare_for_run(
        app_name,
        declare_topology=bool(settings.rabbitmq_url),
    )

    # Migrate schema BEFORE start_consumers — durable consumers (e.g. the
    # world/life event mailbox + intent edges) need their data tables to
    # exist before the source loops start delivering.
    from app.runtime.engine import Runtime

    runtime_for_sources = Runtime(app_name=app_name)
    await runtime_for_sources.migrate_schema()

    # Load skill definitions
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
        from app.messaging.lifecycle import start_messaging
        from app.runtime.durable import start_consumers

        await start_consumers(app_name=app_name)
        logger.info("Runtime durable consumers started for %s", app_name)
        await start_messaging()
        logger.info("messaging started for %s (inboxes + scheduled delivery)", app_name)

    from app.runtime.http_source import register_http_sources

    register_http_sources(app)
    logger.info("dataflow http sources registered")

    # Phase 4: start the interval source loops + watchdog.
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
        from app.messaging.lifecycle import stop_messaging
        from app.runtime.durable import stop_consumers

        await stop_messaging()
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
