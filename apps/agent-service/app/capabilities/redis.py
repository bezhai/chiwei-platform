"""Redis capability — plan B5.

Wraps a raw ``redis.asyncio.Redis`` client behind a domain-shaped API.
Business code never reaches into the raw client; every read/write goes
through this capability so:

* Keys auto-prefix with ``{lane}:`` when ``current_lane()`` returns a
  non-prod lane name. Prod (``current_lane() is None``) leaves keys bare
  so existing prod data isn't migrated.
* Raw redis failures map to the typed ``CapabilityCallFailed`` /
  ``CapabilityTimeout`` exceptions (contract §4.8) — no naked
  ``redis.RedisError`` escapes capability boundaries.
* ``pipeline()`` returns a lane-aware proxy that applies the same key
  prefix to every queued op.

Used by ``infra/image.py`` (registry Lua), debounce / single-flight
runtime, and the ``banned_words`` capability. The cutover of those
call-sites is plan item C5 — B5 only ships the capability + the
two-lane dedup-Lua acceptance scenario.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import redis.exceptions
from redis.asyncio import Redis
from redis.asyncio.client import Pipeline as _RawPipeline

from app.capabilities._errors import CapabilityCallFailed, CapabilityTimeout
from app.infra.rabbitmq import current_lane

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Key prefixing
# ---------------------------------------------------------------------------


def _prefix(key: str) -> str:
    """Return ``{lane}:{key}`` for non-prod lanes, bare ``key`` for prod."""
    lane = current_lane()
    return f"{lane}:{key}" if lane else key


def _prefix_all(keys: list[str]) -> list[str]:
    lane = current_lane()
    if not lane:
        return list(keys)
    return [f"{lane}:{k}" for k in keys]


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
# Pipeline proxy
# ---------------------------------------------------------------------------


class _LanePipeline:
    """Pipeline wrapper that auto-prefixes keys for every queued op.

    Mirrors the small subset of ``redis.asyncio.client.Pipeline`` methods
    the codebase actually queues today (incr / eval / hset / set / expire).
    Add more proxied methods as call-sites need them — keeping the surface
    small forces consumers to come back to this file when they need
    something new, rather than silently growing a god-pipeline.
    """

    def __init__(self, raw: _RawPipeline) -> None:
        self._raw = raw

    def incr(self, key: str, amount: int = 1) -> "_LanePipeline":
        self._raw.incr(_prefix(key), amount)
        return self

    def set(self, key: str, value: Any, **kw: Any) -> "_LanePipeline":
        self._raw.set(_prefix(key), value, **kw)
        return self

    def hset(self, key: str, *a: Any, **kw: Any) -> "_LanePipeline":
        self._raw.hset(_prefix(key), *a, **kw)
        return self

    def expire(self, key: str, seconds: int) -> "_LanePipeline":
        self._raw.expire(_prefix(key), seconds)
        return self

    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> "_LanePipeline":
        # numkeys keys, then args. Prefix the first numkeys positional args.
        keys = list(keys_and_args[:numkeys])
        args = list(keys_and_args[numkeys:])
        prefixed = _prefix_all(keys)
        self._raw.eval(script, numkeys, *prefixed, *args)
        return self

    async def execute(self) -> list[Any]:
        try:
            return await self._raw.execute()
        except (
            asyncio.TimeoutError,
            redis.exceptions.RedisError,
        ) as e:
            raise _wrap_error(e, op="pipeline.execute") from e


# ---------------------------------------------------------------------------
# Public capability
# ---------------------------------------------------------------------------


class RedisCapability:
    """Lane-aware Redis adapter.

    Construct one per process and inject; modules that want a singleton
    should reach for ``app.capabilities.redis.redis_capability`` (lazy
    accessor below). Keys passed in are *logical* — the capability adds
    the lane prefix transparently.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client

    # -- atomic counters -----------------------------------------------------

    async def incr(self, key: str, amount: int = 1) -> int:
        prefixed = _prefix(key)
        try:
            return await self._client.incr(prefixed, amount)
        except (
            asyncio.TimeoutError,
            redis.exceptions.RedisError,
        ) as e:
            raise _wrap_error(e, op="incr", key=prefixed) from e

    # -- Hash + Set read accessors ------------------------------------------

    async def hget(self, key: str, field: str) -> Any:
        """``HGET key field`` — lane-prefixed; returns ``None`` if missing."""
        prefixed = _prefix(key)
        try:
            return await self._client.hget(prefixed, field)
        except (
            asyncio.TimeoutError,
            redis.exceptions.RedisError,
        ) as e:
            raise _wrap_error(e, op="hget", key=prefixed) from e

    async def hgetall(self, key: str) -> dict[str, Any]:
        """``HGETALL key`` — lane-prefixed; returns ``{}`` if missing."""
        prefixed = _prefix(key)
        try:
            return await self._client.hgetall(prefixed)
        except (
            asyncio.TimeoutError,
            redis.exceptions.RedisError,
        ) as e:
            raise _wrap_error(e, op="hgetall", key=prefixed) from e

    async def smembers(self, key: str) -> set[Any]:
        """``SMEMBERS key`` — lane-prefixed; returns empty set if missing."""
        prefixed = _prefix(key)
        try:
            return await self._client.smembers(prefixed)
        except (
            asyncio.TimeoutError,
            redis.exceptions.RedisError,
        ) as e:
            raise _wrap_error(e, op="smembers", key=prefixed) from e

    # -- Lua scripts ---------------------------------------------------------

    async def eval(
        self,
        script: str,
        *,
        keys: list[str],
        args: list[Any],
    ) -> Any:
        """Execute a Lua ``EVAL``.

        ``keys`` are auto-prefixed (the script sees the prefixed forms in
        ``KEYS``). ``args`` pass through untouched.
        """
        prefixed = _prefix_all(keys)
        try:
            return await self._client.eval(script, len(prefixed), *prefixed, *args)
        except (
            asyncio.TimeoutError,
            redis.exceptions.RedisError,
        ) as e:
            key_repr = prefixed[0] if prefixed else None
            raise _wrap_error(e, op="eval", key=key_repr) from e

    # -- pipeline ------------------------------------------------------------

    @asynccontextmanager
    async def pipeline(self) -> AsyncIterator[_LanePipeline]:
        """Yield a lane-aware pipeline; auto-rolls back on exception.

        The yielded object queues ops via prefixed keys; ``execute()``
        runs the pipeline and re-maps redis failures to typed exceptions.
        """
        raw = self._client.pipeline(transaction=False)
        try:
            yield _LanePipeline(raw)
        finally:
            await raw.reset()


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
