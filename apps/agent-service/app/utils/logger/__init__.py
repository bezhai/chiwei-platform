"""
Logger module - re-exports from inner_shared.
"""

# Re-export from inner_shared
from inner_shared.logger import (
    CustomJSONFormatter,
    create_logging_config,
    setup_logging,
)

# Keep local log_config for backward compatibility
from .log_config import setup_logging as local_setup_logging

__all__ = [
    "setup_logging",
    "CustomJSONFormatter",
    "create_logging_config",
    "local_setup_logging",
]
