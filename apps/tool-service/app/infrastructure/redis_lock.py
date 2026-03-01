import asyncio
import logging
import uuid

from app.infrastructure.redis_client import get_redis

logger = logging.getLogger(__name__)

# Lua script for atomic release: only delete if value matches
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class RedisLock:
    """Async context manager for Redis distributed lock."""

    def __init__(self, key: str, ttl: int = 60, timeout: float = 30, retry_interval: float = 0.2):
        self.key = key
        self.ttl = ttl
        self.timeout = timeout
        self.retry_interval = retry_interval
        self._value = uuid.uuid4().hex

    async def __aenter__(self) -> "RedisLock":
        redis = get_redis()
        deadline = asyncio.get_event_loop().time() + self.timeout
        while True:
            acquired = await redis.set(self.key, self._value, ex=self.ttl, nx=True)
            if acquired:
                logger.debug(f"Lock acquired: {self.key}")
                return self
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError(f"Failed to acquire lock: {self.key} within {self.timeout}s")
            await asyncio.sleep(self.retry_interval)

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        redis = get_redis()
        await redis.eval(_RELEASE_SCRIPT, 1, self.key, self._value)
        logger.debug(f"Lock released: {self.key}")
