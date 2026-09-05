"""Redis capability — plan B5.

Wraps a raw ``redis.asyncio.Redis`` client behind a domain-shaped API.
Business code never reaches into the raw client; every read/write goes
through this capability so:

* Raw redis failures map to the typed ``CapabilityCallFailed`` /
  ``CapabilityTimeout`` exceptions (contract §4.8) — no naked
  ``redis.RedisError`` escapes capability boundaries.

**No implicit lane key prefix.** Cross-lane isolation is the
ConfigBundle's job, not the capability's. ``coe-*`` lanes get a
physically separate Redis container (chiwei-test) via
``class_overrides[coe]``; ``ppe-*`` lanes intentionally share prod
Redis ("functional verification against real prod data"). An implicit
``{lane}:`` prefix split agent-service-ppe-refactor and
chat-response-worker between two key spaces and silently dropped
values on lane verification — see trace
``3de371aea10290b327f1386ea56f180c`` and hotfix commit on
2026-05-13.

The runtime's debounce / single-flight modules talk to the raw client
directly to avoid inverting the dependency stack (capability →
runtime).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import redis.exceptions
from redis.asyncio import Redis

from app.capabilities._errors import CapabilityCallFailed, CapabilityTimeout

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _wrap_error(exc: BaseException, *, op: str, key: str | None = None) -> Exception:
    """Map raw redis / asyncio errors to typed capability exceptions."""
    meta: dict[str, Any] = {"op": op}
    if key is not None:
        meta["key"] = key

    if isinstance(exc, asyncio.TimeoutError):
        return CapabilityTimeout(f"redis {op} timeout", meta=meta)
    if isinstance(exc, redis.exceptions.TimeoutError):
        return CapabilityTimeout(f"redis {op} timeout: {exc}", meta=meta)
    if isinstance(exc, redis.exceptions.RedisError):
        # ConnectionError is a subclass of RedisError, lumped under CallFailed.
        return CapabilityCallFailed(f"redis {op} failed: {exc}", meta=meta)
    # Fall through: not our domain — let it propagate untouched.
    return exc


# ---------------------------------------------------------------------------
# Public capability
# ---------------------------------------------------------------------------


class RedisCapability:
    """Domain-shaped Redis adapter.

    Construct one per process and inject; modules that want a singleton
    should reach for ``get_redis_capability()`` below. Keys are passed
    to the underlying client verbatim — cross-lane isolation is the
    ConfigBundle's job (see module docstring).
    """

    def __init__(self, client: Redis) -> None:
        self._client = client

    # -- atomic counters -----------------------------------------------------

    async def incr(self, key: str, amount: int = 1) -> int:
        try:
            return await self._client.incr(key, amount)
        except (
            asyncio.TimeoutError,
            redis.exceptions.RedisError,
        ) as e:
            raise _wrap_error(e, op="incr", key=key) from e

    # -- Set read accessor ---------------------------------------------------

    async def smembers(self, key: str) -> set[Any]:
        """``SMEMBERS key`` — returns empty set if missing."""
        try:
            return await self._client.smembers(key)
        except (
            asyncio.TimeoutError,
            redis.exceptions.RedisError,
        ) as e:
            raise _wrap_error(e, op="smembers", key=key) from e

    # -- String value with TTL (agent session续接) ---------------------------

    async def get(self, key: str) -> Any:
        """``GET key`` — returns ``None`` if missing."""
        try:
            return await self._client.get(key)
        except (
            asyncio.TimeoutError,
            redis.exceptions.RedisError,
        ) as e:
            raise _wrap_error(e, op="get", key=key) from e

    async def set_with_ttl(self, key: str, value: str, *, ttl_seconds: int) -> None:
        """``SET key value EX ttl_seconds`` — value + expiry set atomically.

        Resets the key's TTL on every write (the session store relies on this
        to refresh the 24h window each round).
        """
        try:
            await self._client.set(key, value, ex=ttl_seconds)
        except (
            asyncio.TimeoutError,
            redis.exceptions.RedisError,
        ) as e:
            raise _wrap_error(e, op="set_with_ttl", key=key) from e

    async def expire(self, key: str, seconds: int) -> None:
        """``EXPIRE key seconds`` — refresh a key's TTL without rewriting it."""
        try:
            await self._client.expire(key, seconds)
        except (
            asyncio.TimeoutError,
            redis.exceptions.RedisError,
        ) as e:
            raise _wrap_error(e, op="expire", key=key) from e

    # -- Lua scripts ---------------------------------------------------------

    async def eval(
        self,
        script: str,
        *,
        keys: list[str],
        args: list[Any],
    ) -> Any:
        """Execute a Lua ``EVAL`` with ``keys`` and ``args`` passed
        through untouched."""
        try:
            return await self._client.eval(script, len(keys), *keys, *args)
        except (
            asyncio.TimeoutError,
            redis.exceptions.RedisError,
        ) as e:
            key_repr = keys[0] if keys else None
            raise _wrap_error(e, op="eval", key=key_repr) from e


# ---------------------------------------------------------------------------
# Module-level lazy singleton
# ---------------------------------------------------------------------------


_singleton: RedisCapability | None = None


async def get_redis_capability() -> RedisCapability:
    """Return the process-wide ``RedisCapability``.

    Lazily wraps the existing ``app.infra.redis.get_redis()`` client so
    business modules don't have to thread the capability through every
    call-site during the B5 → C5 migration.
    """
    global _singleton  # noqa: PLW0603
    if _singleton is None:
        from app.infra.redis import get_redis

        _singleton = RedisCapability(await get_redis())
    return _singleton


__all__ = [
    "RedisCapability",
    "get_redis_capability",
]
