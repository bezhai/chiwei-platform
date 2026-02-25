"""
Middlewares module - re-exports from inner_shared.
"""

# Re-export from inner_shared
from inner_shared.middlewares import get_app_name, get_header_var, get_trace_id

# Keep local middleware for backward compatibility
from .trace import HeaderContextMiddleware

__all__ = [
    "HeaderContextMiddleware",
    "get_trace_id",
    "get_app_name",
    "get_header_var",
]
