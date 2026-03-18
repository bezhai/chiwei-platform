"""ImageRegistry — 每请求图片编号注册表

基于 Redis Hash，维护 N.png → TOS URL 的映射。
写入方: agent-service (context_builder + tools)
读取方: chat-response-worker (发送前解析 @N.png)

Redis key:  image_registry:{message_id}
Fields:     __counter__ → N, 1.png → tos_url, 2.png → tos_url, ...
TTL:        30 分钟
"""

import logging

from redis.asyncio import Redis

from app.clients.redis import AsyncRedisClient

logger = logging.getLogger(__name__)

_TTL = 30 * 60  # 30 minutes

# Lua script: atomic HINCRBY + HSET + EXPIRE
# Returns the new counter value
_REGISTER_LUA = """
local key = KEYS[1]
local url = ARGV[1]
local ttl = tonumber(ARGV[2])

local n = redis.call('HINCRBY', key, '__counter__', 1)
local filename = n .. '.png'
redis.call('HSET', key, filename, url)
redis.call('EXPIRE', key, ttl)
return n
"""


class ImageRegistry:
    """Per-request image registry backed by Redis Hash."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        self._key = f"image_registry:{message_id}"
        self._redis: Redis = AsyncRedisClient.get_instance()

    async def register(self, tos_url: str) -> str:
        """Register a TOS URL, return filename like '3.png'."""
        n = await self._redis.eval(_REGISTER_LUA, 1, self._key, tos_url, _TTL)
        filename = f"{n}.png"
        logger.debug(f"Registered image: {filename} -> {tos_url[:80]}...")
        return filename

    async def register_batch(self, urls: list[str]) -> list[str]:
        """Register multiple URLs atomically via pipeline."""
        if not urls:
            return []

        pipe = self._redis.pipeline(transaction=False)
        for url in urls:
            pipe.eval(_REGISTER_LUA, 1, self._key, url, _TTL)
        results = await pipe.execute()

        filenames = []
        for n in results:
            filename = f"{n}.png"
            filenames.append(filename)

        logger.debug(f"Batch registered {len(filenames)} images for {self.message_id}")
        return filenames

    async def resolve(self, filename: str) -> str | None:
        """Resolve a filename to its TOS URL."""
        return await self._redis.hget(self._key, filename)

    async def resolve_all(self) -> dict[str, str]:
        """Get all filename -> URL mappings (excludes __counter__)."""
        data = await self._redis.hgetall(self._key)
        data.pop("__counter__", None)
        return data
