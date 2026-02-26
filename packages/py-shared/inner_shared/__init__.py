"""
inner-shared: Shared Python utilities for inner-bot-server.
"""

# Decorators
from .decorators import (
    AbstractRedisClient,
    auto_json_serialize,
    create_redis_cache,
    dict_serialize,
    handle_errors,
    log_io,
)

# Middlewares
from .middlewares import (
    create_header_context_middleware,
    get_app_name,
    get_header_var,
    get_trace_id,
)

# Logger
from .logger import CustomJSONFormatter, create_logging_config, setup_logging

# Patterns
from .patterns import AsyncInitializable, AsyncSingletonFactory

# Utils
from .utils import (
    AsyncIntervalChecker,
    BackfillWindow,
    BackfillWindowGenerator,
    TimeRangeParser,
    split_time,
)

__all__ = [
    # Decorators
    "log_io",
    "dict_serialize",
    "auto_json_serialize",
    "handle_errors",
    "create_redis_cache",
    "AbstractRedisClient",
    # Middlewares
    "create_header_context_middleware",
    "get_header_var",
    "get_trace_id",
    "get_app_name",
    # Logger
    "CustomJSONFormatter",
    "create_logging_config",
    "setup_logging",
    # Patterns
    "AsyncInitializable",
    "AsyncSingletonFactory",
    # Utils
    "AsyncIntervalChecker",
    "TimeRangeParser",
    "BackfillWindowGenerator",
    "BackfillWindow",
    "split_time",
    # Misc
    "hello",
]

__version__ = "1.0.0"


def hello() -> str:
    """Return a greeting message for testing package import."""
    return f"inner-shared v{__version__}"
