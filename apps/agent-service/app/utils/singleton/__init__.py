"""
Singleton module - re-exports from inner_shared.
"""

# Re-export from inner_shared
from inner_shared.patterns import AsyncInitializable, AsyncSingletonFactory

__all__ = [
    "AsyncInitializable",
    "AsyncSingletonFactory",
]
