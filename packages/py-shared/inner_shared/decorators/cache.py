"""
Abstract cache decorator.
Requires a Redis client implementation to be provided.
"""

import functools
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable)


class AbstractRedisClient(ABC):
    """Abstract Redis client interface for cache decorator."""

    @abstractmethod
    async def get(self, key: str) -> str | None:
        """Get value by key."""
        pass

    @abstractmethod
    async def setex(self, key: str, seconds: int, value: str) -> None:
        """Set value with expiration."""
        pass


def create_redis_cache(
    redis_client_getter: Callable[[], AbstractRedisClient],
    key_prefix: str = "cache",
    expire_seconds: int = 86400,
) -> Callable[[F], F]:
    """
    Create a Redis cache decorator factory.

    Args:
        redis_client_getter: Function that returns the Redis client instance
        key_prefix: Prefix for cache keys
        expire_seconds: Cache expiration time in seconds (default 24 hours)

    Returns:
        A decorator function
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            cache_key = _generate_cache_key(key_prefix, func.__name__, args, kwargs)
            redis_client = redis_client_getter()

            try:
                cached_result = await redis_client.get(cache_key)
                if cached_result:
                    logger.info(f"Cache hit: {func.__name__}, key: {cache_key}")
                    return json.loads(cached_result)

                logger.info(f"Cache miss: {func.__name__}, key: {cache_key}")
                result = await func(*args, **kwargs)

                await redis_client.setex(
                    cache_key,
                    expire_seconds,
                    json.dumps(result, ensure_ascii=False, default=str),
                )
                logger.info(f"Result cached: {func.__name__}, key: {cache_key}, expire: {expire_seconds}s")

                return result

            except Exception as e:
                logger.error(f"Cache operation failed: {func.__name__}, error: {str(e)}")
                return await func(*args, **kwargs)

        return wrapper  # type: ignore

    return decorator


def _generate_cache_key(prefix: str, func_name: str, args: tuple, kwargs: dict[str, Any]) -> str:
    """
    Generate cache key from function name and arguments.

    Args:
        prefix: Key prefix
        func_name: Function name
        args: Positional arguments
        kwargs: Keyword arguments

    Returns:
        Cache key string
    """
    cache_data = {"func": func_name, "args": args, "kwargs": kwargs}
    cache_str = json.dumps(cache_data, sort_keys=True, ensure_ascii=False, default=str)
    cache_hash = hashlib.md5(cache_str.encode("utf-8")).hexdigest()
    return f"{prefix}:{func_name}:{cache_hash}"
