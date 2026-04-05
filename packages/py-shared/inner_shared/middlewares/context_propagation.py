"""
Generic x-ctx-* context propagation middleware for FastAPI.

Separate from the existing trace.py header system — this handles
dynamic x-ctx-* headers that flow between services for lane routing,
gray groups, and other cross-cutting concerns.
"""

import contextvars
from collections.abc import Callable

CTX_PREFIX = "x-ctx-"

ctx_vars: dict[str, contextvars.ContextVar[str | None]] = {}


def _get_or_create_var(name: str) -> contextvars.ContextVar[str | None]:
    """Get or lazily create a context var for the given header name."""
    if name not in ctx_vars:
        ctx_vars[name] = contextvars.ContextVar(f"ctx_{name}", default=None)
    return ctx_vars[name]


def get_context_headers() -> dict[str, str]:
    """Read all stored x-ctx-* values, return dict for outbound requests."""
    headers: dict[str, str] = {}
    for header_name, var in ctx_vars.items():
        value = var.get()
        if value is not None:
            headers[header_name] = value
    return headers


def create_context_propagation_middleware():
    """
    Create a ContextPropagationMiddleware class for FastAPI.

    Returns:
        ContextPropagationMiddleware class to be used with app.add_middleware()
    """
    try:
        from starlette.middleware.base import BaseHTTPMiddleware
        from fastapi import Request, Response
    except ImportError:
        raise ImportError("FastAPI/Starlette required for ContextPropagationMiddleware")

    class ContextPropagationMiddleware(BaseHTTPMiddleware):
        """
        Inbound: extract all x-ctx-* headers, store in contextvars.
        Outbound: echo x-ctx-* headers back in response headers.
        """

        async def dispatch(self, request: Request, call_next: Callable) -> Response:
            # Extract inbound x-ctx-* headers
            for key, value in request.headers.items():
                if key.lower().startswith(CTX_PREFIX):
                    var = _get_or_create_var(key.lower())
                    var.set(value)

            response = await call_next(request)

            # Echo x-ctx-* headers in response
            for header_name, var in ctx_vars.items():
                value = var.get()
                if value is not None:
                    response.headers[header_name] = value

            return response

    return ContextPropagationMiddleware
