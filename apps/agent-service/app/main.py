import asyncio
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from inner_shared import hello as shared_hello
from inner_shared.logger import setup_logging

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
        from app.workers.chat_consumer import start_chat_consumer
        from app.workers.post_consumer import start_post_consumer

        consumer_tasks.append(asyncio.create_task(start_post_consumer()))
        logger.info("Post safety consumer started")

        consumer_tasks.append(asyncio.create_task(start_chat_consumer()))
        logger.info("Chat request consumer started")

    yield

    # Shutdown consumers
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
from inner_shared.middlewares.context_propagation import create_context_propagation_middleware
app.add_middleware(create_context_propagation_middleware())

# Register routes
app.include_router(api_router)
