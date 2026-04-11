"""Async Redis client — module-level lazy singleton via ``get_redis()``."""

from __future__ import annotations

import asyncio

from redis.asyncio import ConnectionPool, Redis

from app.infra.config import settings

_redis: Redis | None = None
_lock = asyncio.Lock()


async def get_redis() -> Redis:
    """Return a shared async Redis client, creating it on first call."""
    global _redis  # noqa: PLW0603
    if _redis is not None:
        return _redis

    async with _lock:
        # Double-check after acquiring lock
        if _redis is not None:
            return _redis

        pool = ConnectionPool(
            host=settings.redis_host,
            port=6379,
            password=settings.redis_password,
            decode_responses=True,
            max_connections=10,
        )
        _redis = Redis(connection_pool=pool)
        return _redis
