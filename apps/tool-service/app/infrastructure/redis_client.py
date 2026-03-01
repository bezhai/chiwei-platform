import logging

from redis.asyncio import ConnectionPool, Redis

from app.config.config import settings

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None
_client: Redis | None = None


async def init_redis() -> None:
    global _pool, _client
    _pool = ConnectionPool(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password,
        decode_responses=True,
        max_connections=10,
    )
    _client = Redis(connection_pool=_pool)
    logger.info("Redis client initialized")


async def close_redis() -> None:
    global _client, _pool
    if _client:
        await _client.aclose()
        _client = None
    if _pool:
        await _pool.disconnect()
        _pool = None
    logger.info("Redis client closed")


def get_redis() -> Redis:
    if _client is None:
        raise RuntimeError("Redis client not initialized, call init_redis() first")
    return _client


async def redis_get(key: str) -> str | None:
    return await get_redis().get(key)


async def redis_set_with_expire(key: str, value: str, seconds: int) -> None:
    await get_redis().set(key, value, ex=seconds)
