"""In-process scheduled task pool (Gap 9.2 best_effort fallback for emit_delayed).

WARNING: tasks are tracked in this process only. Runtime stop / pod
restart / deploy cancels all pending tasks → events are lost. Callers
MUST opt in via ``emit_delayed(..., durability="best_effort")``; the
default ``durable`` path goes through the
``runtime_delayed_trigger_{app}`` queue (Task 8/9) and survives restart.

Lifecycle:

- ``schedule_after(delay, callable_)`` returns the asyncio.Task tracking
  the deferred run. The task is added to ``SCHEDULED_TASKS`` and
  removed automatically on completion (success or cancellation).
- ``cancel_all_scheduled()`` is called from
  ``Runtime.stop_source_loops`` so a graceful shutdown does not leak
  pending background tasks into the next process instance.
- Exceptions from ``callable_`` are logged and swallowed —
  best_effort means we do not propagate failures into the calling
  context (which has already returned).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

SCHEDULED_TASKS: set[asyncio.Task] = set()


async def schedule_after(
    delay: float,
    callable_: Callable[[], Awaitable[None]],
) -> asyncio.Task:
    async def runner() -> None:
        try:
            await asyncio.sleep(delay)
            await callable_()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduled task raised; swallowing")

    task = asyncio.create_task(runner())
    SCHEDULED_TASKS.add(task)
    task.add_done_callback(SCHEDULED_TASKS.discard)
    return task


def cancel_all_scheduled() -> int:
    """Cancel all pending scheduled tasks. Returns count cancelled."""
    pending = [t for t in SCHEDULED_TASKS if not t.done()]
    for t in pending:
        t.cancel()
    SCHEDULED_TASKS.clear()
    return len(pending)
