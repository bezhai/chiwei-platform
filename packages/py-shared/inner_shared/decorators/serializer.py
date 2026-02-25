"""
Pydantic serialization decorators.
Supports both sync and async functions.
"""

import functools
import json
import logging
from collections.abc import AsyncGenerator
from inspect import iscoroutinefunction
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable)


def dict_serialize(func: F) -> F:
    """Decorator: automatically serialize Pydantic model return values to dict."""

    if iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            result = await func(*args, **kwargs)
            return _serialize_dict(result)

        return async_wrapper  # type: ignore
    else:

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            return _serialize_dict(result)

        return sync_wrapper  # type: ignore


def _serialize_dict(data: Any) -> Any:
    """Recursively serialize Pydantic models in dict."""
    if isinstance(data, BaseModel):
        return data.model_dump()
    elif isinstance(data, list):
        return [item.model_dump() if isinstance(item, BaseModel) else item for item in data]
    elif isinstance(data, dict):
        return {key: _serialize_dict(value) for key, value in data.items()}
    return data


def auto_json_serialize(func: Callable[..., AsyncGenerator[Any, None]]) -> Callable[..., AsyncGenerator[str, None]]:
    """
    Auto-serialize decorator for async generators.
    Converts Pydantic models to JSON strings.
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs) -> AsyncGenerator[str, None]:
        async for item in func(*args, **kwargs):
            try:
                if hasattr(item, "model_dump_json"):
                    yield item.model_dump_json()
                elif hasattr(item, "json"):  # Pydantic v1 compatibility
                    yield item.json()
                elif isinstance(item, dict):
                    yield json.dumps(item, ensure_ascii=False)
                else:
                    yield str(item)
            except Exception as e:
                logger.error(f"Serialization failed: {e}, item: {item}")
                yield str(item)

    return wrapper
