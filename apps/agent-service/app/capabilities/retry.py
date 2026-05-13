"""``@retry`` — process-local retry decorator for async network calls.

Sibling abstraction to ``app/runtime/retry.py`` but a different scope:

* ``app/runtime/retry.py`` decides whether a *durable* RabbitMQ message
  should be re-delivered across processes (broker-layer redelivery).
* ``@retry`` here is in-process. It wraps an ``async def`` and retries
  the call locally with exponential / linear backoff against a
  white-list of exception types — used for one-shot HTTP / LLM calls
  where transient timeouts and 429s benefit from retrying inside the
  same task rather than dropping the request to DLQ.

Defaults match the typed capability layer (A3 / contract §4.8): the
white-list is the three retry-eligible classes ``CapabilityTimeout``,
``CapabilityRateLimited``, ``CapabilityCallFailed``; the two LLM-visible
classes ``CapabilityInvalidArg`` and ``CapabilityNotFound`` are *not*
retried because retrying them never succeeds — they signal the caller
asked for the wrong thing.

Example::

    @retry(attempts=3)
    async def call_llm(...) -> str:
        return await client.complete(...)
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from app.capabilities._errors import (
    CapabilityCallFailed,
    CapabilityRateLimited,
    CapabilityTimeout,
)

_logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


_DEFAULT_RETRY_ON: tuple[type[Exception], ...] = (
    CapabilityTimeout,
    CapabilityRateLimited,
    CapabilityCallFailed,
)


def _compute_delay(
    *, attempt: int, backoff: str, base_delay_s: float, max_delay_s: float
) -> float:
    """Delay before the *next* attempt, given the just-failed ``attempt`` (1-indexed)."""
    if backoff == "exponential":
        raw = base_delay_s * (2 ** (attempt - 1))
    elif backoff == "linear":
        raw = base_delay_s * attempt
    else:  # pragma: no cover — validated up-front in ``retry``
        raise ValueError(f"unsupported backoff strategy: {backoff!r}")
    return min(raw, max_delay_s)


def retry(
    *,
    attempts: int = 3,
    backoff: str = "exponential",
    base_delay_s: float = 0.5,
    max_delay_s: float = 30.0,
    retry_on: tuple[type[Exception], ...] = _DEFAULT_RETRY_ON,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Retry an ``async def`` call against a white-list of exception types.

    Args:
        attempts: Total number of attempts (must be ``>= 1``). ``attempts=1``
            disables retrying — the function runs once and any exception
            propagates.
        backoff: ``"exponential"`` → ``base * 2^(N-1)``;
            ``"linear"`` → ``base * N``, where ``N`` is the attempt number
            that just failed (1-indexed).
        base_delay_s: Multiplier for the backoff formula (seconds).
        max_delay_s: Clamp for the computed delay (seconds).
        retry_on: Exception classes that trigger a retry. Anything else
            propagates immediately. Defaults to the three retry-eligible
            capability exceptions (timeout / rate-limited / call-failed).

    Returns:
        A decorator that preserves ``__wrapped__`` / ``__name__`` / ``__doc__``.

    Raises:
        ValueError: if ``attempts < 1`` or ``backoff`` is not a known strategy.
        TypeError: at decoration time, if the wrapped object is not an
            ``async def`` (sync functions cannot ``await asyncio.sleep``).
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    if backoff not in ("exponential", "linear"):
        raise ValueError(
            f"backoff must be 'exponential' or 'linear', got {backoff!r}"
        )

    def _decorator(
        func: Callable[P, Awaitable[R]],
    ) -> Callable[P, Awaitable[R]]:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(
                f"@retry only wraps async functions; {func!r} is sync. "
                "Wrap an `async def` instead."
            )

        @functools.wraps(func)
        async def _wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            last_exc: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except retry_on as exc:
                    last_exc = exc
                    if attempt >= attempts:
                        raise
                    delay = _compute_delay(
                        attempt=attempt,
                        backoff=backoff,
                        base_delay_s=base_delay_s,
                        max_delay_s=max_delay_s,
                    )
                    _logger.warning(
                        "%s attempt %d/%d failed (%s: %s); retrying in %.2fs",
                        func.__qualname__,
                        attempt,
                        attempts,
                        type(exc).__name__,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
            # Unreachable: loop either returned or re-raised on the last attempt.
            raise RuntimeError("retry loop exhausted without return") from last_exc

        return _wrapper

    return _decorator


__all__ = ["retry"]
