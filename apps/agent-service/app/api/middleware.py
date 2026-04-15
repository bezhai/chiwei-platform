"""HTTP middleware — metrics (Prometheus) + header context (trace/lane).

Merges the old ``middleware/metrics.py``, ``middleware/chat_metrics.py``,
and ``utils/middlewares/trace.py`` into one module.
"""

from __future__ import annotations

import contextvars
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import Request, Response
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# ---------------------------------------------------------------------------
# Prometheus — HTTP request metrics
# ---------------------------------------------------------------------------

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
)
REQUEST_IN_FLIGHT = Gauge(
    "http_requests_in_flight",
    "Number of HTTP requests currently being processed",
)

# ---------------------------------------------------------------------------
# Prometheus — chat pipeline metrics
# ---------------------------------------------------------------------------

PIPELINE_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)

CHAT_PIPELINE_DURATION = Histogram(
    "chat_pipeline_duration_seconds",
    "Duration of each chat pipeline stage",
    ["stage"],
    buckets=PIPELINE_BUCKETS,
)

CHAT_FIRST_TOKEN = Histogram(
    "chat_first_token_seconds",
    "Time to first token from agent stream",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30),
)

CHAT_TOKENS = Counter(
    "chat_tokens_total",
    "Token count by type",
    ["type"],
)

CHAT_QUEUE_WAIT = Histogram(
    "chat_queue_wait_seconds",
    "Time spent waiting in MQ queue (chat_request)",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)


# ---------------------------------------------------------------------------
# PrometheusMiddleware (ASGI)
# ---------------------------------------------------------------------------


class PrometheusMiddleware:
    """ASGI middleware that records HTTP metrics."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = StarletteRequest(scope)

        # Serve /metrics endpoint directly
        if request.url.path == "/metrics":
            body = generate_latest()
            response = StarletteResponse(
                body, media_type="text/plain; version=0.0.4; charset=utf-8"
            )
            await response(scope, receive, send)
            return

        method = request.method
        status_code = 500
        # Prefer route template path to avoid high-cardinality labels
        route = scope.get("route")
        path = route.path if route and hasattr(route, "path") else request.url.path

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        REQUEST_IN_FLIGHT.inc()
        start = time.monotonic()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.monotonic() - start
            REQUEST_IN_FLIGHT.dec()
            REQUEST_COUNT.labels(
                method=method, path=path, status=str(status_code)
            ).inc()
            REQUEST_DURATION.labels(method=method, path=path).observe(duration)


# ---------------------------------------------------------------------------
# Header context (contextvars) — trace_id, app_name, lane
# ---------------------------------------------------------------------------

header_vars: dict[str, contextvars.ContextVar[Any]] = {}

HEADER_CONFIG: dict[str, dict[str, Any]] = {
    "X-Trace-Id": {
        "var_name": "trace_id",
        "default_factory": lambda: str(uuid.uuid4()),
        "required": True,
    },
    "X-App-Name": {
        "var_name": "app_name",
        "default_factory": lambda: None,
        "required": False,
    },
    "x-ctx-lane": {
        "var_name": "lane",
        "default_factory": lambda: None,
        "required": False,
    },
}

for _header_name, _cfg in HEADER_CONFIG.items():
    _var_name = _cfg["var_name"]
    header_vars[_var_name] = contextvars.ContextVar(_var_name, default=None)


class HeaderContextMiddleware(BaseHTTPMiddleware):
    """Read trace/lane headers into contextvars, echo them back in response."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        for header_name, config in HEADER_CONFIG.items():
            var_name = config["var_name"]
            header_value = request.headers.get(header_name)

            if not header_value and config["default_factory"]:
                header_value = config["default_factory"]()

            header_vars[var_name].set(header_value)

        response = await call_next(request)

        for header_name, config in HEADER_CONFIG.items():
            var_name = config["var_name"]
            value = header_vars[var_name].get()
            if value is not None:
                response.headers[header_name] = str(value)

        return response


# ---------------------------------------------------------------------------
# Convenience accessors (used throughout the codebase)
# ---------------------------------------------------------------------------


def get_header_var(var_name: str) -> Any:
    """Get a header context variable by name."""
    var = header_vars.get(var_name)
    return var.get() if var else None


def get_trace_id() -> str | None:
    """Get current request trace_id."""
    return get_header_var("trace_id")


def get_app_name() -> str | None:
    """Get current request app_name."""
    return get_header_var("app_name")


def get_lane() -> str | None:
    """Get current request lane."""
    return get_header_var("lane")
