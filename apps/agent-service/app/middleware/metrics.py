"""Prometheus metrics middleware for FastAPI (ASGI)."""

import time

from prometheus_client import Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

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


class PrometheusMiddleware:
    """ASGI middleware that records HTTP metrics."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)

        # Serve /metrics endpoint directly
        if request.url.path == "/metrics":
            body = generate_latest()
            response = Response(
                body, media_type="text/plain; version=0.0.4; charset=utf-8"
            )
            await response(scope, receive, send)
            return

        path = request.url.path
        method = request.method
        status_code = 500

        # Capture status code from response
        async def send_wrapper(message):
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
