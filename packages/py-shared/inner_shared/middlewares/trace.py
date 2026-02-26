"""
Header Context middleware for FastAPI.
Manages request header variables using contextvars.
"""

import contextvars
import uuid
from collections.abc import Callable
from typing import Any

# Store all header variables
header_vars: dict[str, contextvars.ContextVar[Any]] = {}

# Default header configuration
DEFAULT_HEADER_CONFIG = {
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
}


def init_header_vars(config: dict | None = None) -> None:
    """Initialize context variables from config."""
    global header_vars
    header_config = config or DEFAULT_HEADER_CONFIG
    for _header_name, cfg in header_config.items():
        var_name = cfg["var_name"]
        if var_name not in header_vars:
            header_vars[var_name] = contextvars.ContextVar(var_name, default=None)


# Initialize with default config
init_header_vars()


def create_header_context_middleware(config: dict | None = None):
    """
    Create a HeaderContextMiddleware class for FastAPI.

    Args:
        config: Optional header configuration dict

    Returns:
        HeaderContextMiddleware class
    """
    header_config = config or DEFAULT_HEADER_CONFIG

    # Ensure vars are initialized
    init_header_vars(header_config)

    try:
        from starlette.middleware.base import BaseHTTPMiddleware
        from fastapi import Request, Response

        class HeaderContextMiddleware(BaseHTTPMiddleware):
            """
            Header Context Middleware

            Features:
            1. Read configured header variables from HTTP request headers
            2. Generate default values for missing required variables
            3. Store all variables in context for global access
            4. Return all variables in response headers
            """

            async def dispatch(self, request: Request, call_next: Callable) -> Response:
                # Process all configured headers
                for header_name, cfg in header_config.items():
                    var_name = cfg["var_name"]
                    header_value = request.headers.get(header_name)

                    # Generate default value if missing and required
                    if not header_value and cfg.get("default_factory"):
                        header_value = cfg["default_factory"]()

                    # Store in context variable
                    header_vars[var_name].set(header_value)

                # Continue processing request
                response = await call_next(request)

                # Add all variables to response headers
                for header_name, cfg in header_config.items():
                    var_name = cfg["var_name"]
                    value = header_vars[var_name].get()
                    if value is not None:
                        response.headers[header_name] = str(value)

                return response

        return HeaderContextMiddleware

    except ImportError:
        raise ImportError("FastAPI/Starlette is required for HeaderContextMiddleware")


def get_header_var(var_name: str) -> Any:
    """
    Get the value of a header variable.

    Args:
        var_name: Variable name (e.g., 'trace_id', 'app_name')

    Returns:
        Variable value, or None if not found
    """
    var = header_vars.get(var_name)
    return var.get() if var else None


def get_trace_id() -> str | None:
    """Get the current request's trace ID."""
    return get_header_var("trace_id")


def get_app_name() -> str | None:
    """Get the current request's app name."""
    return get_header_var("app_name")
