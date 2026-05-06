"""Shared worker utilities — MQ error handler.

Phase 4 cutover removed for_each_persona / prod_only / cron_error_handler
—— 调度迁到 dataflow graph fan-out + node 自身负责 lane gate 和 error
handling。
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


def mq_error_handler() -> Callable:
    """MQ consumer error handling: log + nack (no requeue).

    If the handler already acked/nacked via ``message.process()``,
    the second nack is safely ignored.
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        async def wrapper(message, *args: P.args, **kwargs: P.kwargs) -> T | None:
            try:
                return await func(message, *args, **kwargs)  # type: ignore[misc]
            except Exception:
                logger.exception("MQ handler %s failed", func.__name__)
                if hasattr(message, "nack"):
                    try:
                        await message.nack(requeue=False)
                    except Exception:
                        pass  # already processed
                return None

        return wrapper  # type: ignore[return-value]

    return decorator
