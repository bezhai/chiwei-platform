"""Shared worker utilities — persona iteration + error handling decorators.

Eliminates the 5x duplicated ``get_all_persona_ids → for → try/except`` pattern.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from app.data.queries import list_all_persona_ids
from app.data.session import get_session
from app.infra.config import settings

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


# ---------------------------------------------------------------------------
# Persona batch iteration
# ---------------------------------------------------------------------------


async def for_each_persona(
    fn: Callable[[str], Awaitable[None]],
    *,
    label: str = "",
) -> None:
    """Iterate all personas with unified error handling.

    Failures for one persona are logged but do not abort the loop.
    """
    async with get_session() as session:
        persona_ids = await list_all_persona_ids(session)
    for persona_id in persona_ids:
        try:
            await fn(persona_id)
        except Exception:
            logger.exception("[%s] %s failed", persona_id, label)


# ---------------------------------------------------------------------------
# Lane guard
# ---------------------------------------------------------------------------


def prod_only(fn: Callable[P, T]) -> Callable[P, T]:
    """Skip execution when running in a non-prod lane.

    Many cron jobs (voice, life-engine, glimpse, dreams, schedules) must only
    run in prod to avoid writing duplicate data from dev lanes.
    """

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
        if settings.lane and settings.lane != "prod":
            return None
        return await fn(*args, **kwargs)  # type: ignore[misc]

    return wrapper  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Error handling decorators
# ---------------------------------------------------------------------------


def cron_error_handler() -> Callable:
    """arq cron job error handling: log + don't interrupt the scheduler."""

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
            try:
                return await func(*args, **kwargs)  # type: ignore[misc]
            except Exception:
                logger.exception("Cron job %s failed", func.__name__)
                return None

        return wrapper  # type: ignore[return-value]

    return decorator


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
