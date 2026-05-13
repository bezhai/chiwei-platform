"""``fan_out_wait`` — process-local concurrency helper (B2).

Replaces hand-rolled ``asyncio.gather(... return_exceptions=True)`` plus
``asyncio.wait_for(...)`` boilerplate scattered across::

    app/chat/_context_images.py     # homogeneous list, no timeout
    app/life/schedule.py            # heterogeneous list, no timeout
    app/nodes/safety.py             # 3 LLM checks, 20s total timeout

Why this exists:

* **Total-deadline semantics with safe cancellation** — when the deadline
  trips, in-flight coroutines are *cancelled* so they cannot leak past the
  caller. The legacy ``asyncio.wait_for(gather(...))`` pattern in
  ``nodes/safety.py`` swallowed the ``TimeoutError`` but never cancelled
  the unfinished tasks; this helper does it correctly while keeping
  fail-open behaviour available via ``return_exceptions=True``.
* **dict input → dict output** — callers that label each branch (e.g.
  ``{"injection": ..., "politics": ..., "nsfw": ...}``) keep their labels
  end-to-end without re-zipping by index.
* **Exception types preserved** — capability errors (``CapabilityTimeout``,
  ``CapabilityRateLimited``, ...) flow through unwrapped so the caller can
  branch on the typed class.

This is *not* a dataflow primitive. For cross-process per-key fan-out the
right tool is the wire-level ``.fan_out_per(...)`` DSL (B7).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar, overload

T = TypeVar("T")


@overload
async def fan_out_wait(
    coros: list[Awaitable[T]],
    *,
    timeout_s: float | None = ...,
    return_exceptions: bool = ...,
) -> list[T | BaseException]: ...


@overload
async def fan_out_wait(
    coros: dict[str, Awaitable[T]],
    *,
    timeout_s: float | None = ...,
    return_exceptions: bool = ...,
) -> dict[str, T | BaseException]: ...


async def fan_out_wait(
    coros: list[Awaitable[T]] | dict[str, Awaitable[T]],
    *,
    timeout_s: float | None = None,
    return_exceptions: bool = True,
) -> list[T | BaseException] | dict[str, T | BaseException]:
    """Run a group of awaitables concurrently and collect their results.

    Args:
        coros: Either a list (results returned in order) or a dict of
            label → awaitable (results returned in a dict by the same
            label). Each awaitable is scheduled as its own task.
        timeout_s: Total deadline in seconds for the whole group. ``None``
            means wait forever. When the deadline trips, awaitables that
            have not finished are cancelled and surface as
            ``TimeoutError`` in their result slot (or raised, depending on
            ``return_exceptions``).
        return_exceptions: ``True`` (default) — exceptions, including
            ``TimeoutError`` from deadline cancellation, are returned in
            the result container so callers can branch on the typed class.
            ``False`` — the first non-cancelled exception is re-raised
            as-is; if only the deadline trips, a ``TimeoutError`` is raised.

    Returns:
        Container matching the input shape (list → list, dict → dict).
        Result slots contain either the awaitable's value or, when
        ``return_exceptions=True``, the exception instance (incl.
        ``TimeoutError`` for deadline-cancelled tasks).

    Raises:
        Original exception (preserved type, not wrapped) when
        ``return_exceptions=False`` and a task raised.
        ``TimeoutError`` when ``return_exceptions=False`` and the
        deadline tripped before all tasks finished.
    """
    # Normalise to (keys, awaitables) so we can dispatch one tasks list
    # and stitch the result back into the caller's shape at the end.
    is_dict = isinstance(coros, dict)
    if is_dict:
        keys: list[str] | None = list(coros.keys())
        awaitables: list[Awaitable[T]] = list(coros.values())
    else:
        keys = None
        awaitables = list(coros)

    if not awaitables:
        return {} if is_dict else []

    # Wrap each awaitable in a Task so we can cancel cleanly on timeout.
    tasks: list[asyncio.Task[T]] = [asyncio.ensure_future(c) for c in awaitables]

    try:
        # ``return_exceptions=True`` on gather keeps the future from
        # propagating exceptions; we apply ``return_exceptions=False``
        # semantics ourselves below so that TimeoutError handling is
        # consistent regardless of the input shape.
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout_s,
        )
        timed_out = False
    except (asyncio.TimeoutError, TimeoutError):
        # asyncio.wait_for already cancelled pending tasks. Give them a
        # loop turn to observe the cancellation, then collect results.
        timed_out = True

    # Collect each task's outcome. Tasks cancelled by wait_for surface as
    # asyncio.CancelledError → translate to TimeoutError for the caller.
    outcomes: list[T | BaseException] = []
    for task in tasks:
        if task.cancelled():
            outcomes.append(TimeoutError("fan_out_wait deadline exceeded"))
            continue
        exc = task.exception()
        if exc is not None:
            outcomes.append(exc)
        else:
            outcomes.append(task.result())

    if not return_exceptions:
        # Re-raise the first non-cancelled exception, preserving its type
        # and instance identity. If the only failure is deadline timeout,
        # raise a fresh TimeoutError.
        first_exc: BaseException | None = None
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, TimeoutError
            ):
                first_exc = outcome
                break
        if first_exc is not None:
            raise first_exc
        if timed_out:
            raise TimeoutError("fan_out_wait deadline exceeded")

    if is_dict:
        assert keys is not None
        return dict(zip(keys, outcomes, strict=True))
    return outcomes


__all__ = ["fan_out_wait"]
