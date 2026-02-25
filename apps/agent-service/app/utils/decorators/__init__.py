"""
Decorators module - re-exports from inner_shared with local extensions.
"""

# Re-export from inner_shared
from inner_shared.decorators import (
    auto_json_serialize,
    dict_serialize,
    handle_errors,
    log_io,
)

# Keep local cache decorator that uses the app's Redis client
from .cache_decorator import redis_cache

__all__ = [
    "handle_errors",
    "dict_serialize",
    "log_io",
    "redis_cache",
    "auto_json_serialize",
]
