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
    # proactive.py's Bridge calls emit_legacy_message() which dispatches
    # via WIRING_REGISTRY — without this load step the registry would be
    # empty here and the call would silently no-op. The FastAPI main
    # process is a *producer* (proactive Message -> vectorize-worker), so
    # it must also pre-declare the durable routes; otherwise messages
    # publish before the consumer pod has had a chance to declare its
    # queue, and the broker drops them.
    from app.runtime.bootstrap import declare_durable_topology, load_dataflow_graph

    load_dataflow_graph()
    if settings.rabbitmq_url:
        await declare_durable_topology()

    # Load skill definitions
    import os
    from pathlib import Path

    from app.skills.registry import SkillRegistry, skill_reload_loop

    skills_dir = Path(os.environ.get("SKILLS_DIR", str(Path(__file__).parent / "skills" / "definitions")))
    SkillRegistry.load_all(skills_dir)

    # Start hot-reload loop
    reload_task = asyncio.create_task(skill_reload_loop(skills_dir))

    # Start MQ consumers (only when RabbitMQ is configured)
    consumer_tasks: list[asyncio.Task] = []
    if settings.rabbitmq_url:
        # Phase 2: post-safety 改走 runtime durable consumer。旧
        # start_post_consumer 删除（替代为 wire(PostSafetyRequest)
        # .to(run_post_safety).durable()）；runtime 自动按 placement.bind
        # 过滤启动属于本 app 的 consumer。
        from app.runtime.durable import start_consumers
        from app.workers.chat_consumer import start_chat_consumer
        await start_consumers(app_name="agent-service")
        logger.info("Runtime durable consumers started for agent-service")

        consumer_tasks.append(asyncio.create_task(start_chat_consumer()))
        logger.info("Chat request consumer started")

    from app.runtime.http_source import register_http_sources
    register_http_sources(app)
    logger.info("dataflow http sources registered")

    yield

    # Phase 2: stop runtime durable consumers cleanly before tearing
    # down RabbitMQ connection (otherwise late deliveries race with close).
    if settings.rabbitmq_url:
        from app.runtime.durable import stop_consumers
        await stop_consumers()

    # Shutdown legacy consumers (chat consumer)
    for task in consumer_tasks:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception) as e:
            if not isinstance(e, asyncio.CancelledError):
                logger.warning("Consumer task ended with error: %s", e)

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
