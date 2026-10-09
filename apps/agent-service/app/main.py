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
from app.host import Host
from app.infra.config import settings
from app.runtime.placement import DEFAULT_APP

load_dotenv()
setup_logging(log_dir="/logs/agent-service", log_file="app.log")

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the plugins of the app this process serves; stop them at shutdown.

    The app is ``APP_NAME`` (PaaS injects it per Deployment), and its plugins are its manifest in
    :data:`app.deployment.APPS`: only those modules are imported, so one app's process loads none
    of the other's code. The plugin host (:mod:`app.host`) runs every startup phase, the schema,
    the broker (when one is configured), the routes on this app, the background tasks and the
    clocks, and stops them in the shutdown order. The middlewares, ``/metrics`` and ``/health``
    are set on the app at import, below.
    """
    app_name = os.getenv("APP_NAME") or DEFAULT_APP
    logger.info("shared pkg loaded: %s", shared_hello())

    host = Host.for_app(app_name)
    await host.start(
        http=app,
        schema=True,
        mq=bool(settings.rabbitmq_url),
        clocks=True,
        tasks=True,
    )
    logger.info(
        "app %s started: plugins %s", app_name, ", ".join(p.name for p in host.plugins)
    )

    yield

    logger.info("app %s stopping", app_name)
    await host.stop()


app = FastAPI(lifespan=lifespan)

# Prometheus metrics middleware (outermost — records all requests)
app.add_middleware(PrometheusMiddleware)

# Header context middleware (trace_id, app_name, lane)
app.add_middleware(HeaderContextMiddleware)

# x-ctx-* context propagation (for sidecar lane routing)
app.add_middleware(create_context_propagation_middleware())

# Register routes
app.include_router(api_router)
