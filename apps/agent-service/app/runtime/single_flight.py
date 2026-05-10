"""Single-flight lock — Phase 7d Gap 14.

Idiom:
    async with single_flight(f"drift:{chat}:{persona}", ttl=600):
        await _do_work()

**语义**：ttl 时间窗内 single-flight，**不是任务存活期严格互斥**。
- 进入：SETNX + uuid token，已被持有 → raise SingleFlightConflict
- 离开：Lua 比较 token 后 DEL（防误删别人持有的锁）
- TTL 到期前：保护 single-flight
- TTL 到期后：哪怕原 holder 还在跑，新 holder 可以进入；原 holder finally 时
  Lua 比较 token 失败、不会误删新 holder 的锁

调用方负责选择「比业务最坏耗时更大的 ttl」。
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.infra.redis import get_redis

_RELEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


class SingleFlightConflict(Exception):
    """Raised when another holder already owns the key."""

    def __init__(self, key: str) -> None:
        super().__init__(f"single-flight conflict on key={key!r}")
        self.key = key


@asynccontextmanager
async def single_flight(key: str, *, ttl: int) -> AsyncIterator[None]:
    """Acquire single-flight lock; raise SingleFlightConflict if held."""
    redis = await get_redis()
    token = uuid.uuid4().hex
    if not await redis.set(key, token, nx=True, ex=ttl):
        raise SingleFlightConflict(key)
    try:
        yield
    finally:
        await redis.eval(_RELEASE_LUA, 1, key, token)
