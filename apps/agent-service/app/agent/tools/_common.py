"""Shared helpers for agent tools — error handling + image upload."""

from __future__ import annotations

import functools
import logging
from typing import Any

logger = logging.getLogger(__name__)


def tool_error(error_message: str):
    """Decorator: catch exceptions, log, and return a friendly error string.

    Applied to ``@tool`` functions so that tool failures surface as readable
    text instead of crashing the agent loop.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                logger.error("%s failed: %s", func.__name__, exc, exc_info=True)
                return f"{error_message}: {exc}"

        return wrapper

    return decorator


async def upload_and_register(
    source_type: str,
    data: str,
    registry: Any,
) -> tuple[str, str | None]:
    """Upload an image to TOS and optionally register in ImageRegistry.

    Returns ``(tos_url, filename)`` on success, ``(data, None)`` on failure.
    """
    from app.infra.image import image_client

    try:
        tos_url = await image_client.upload_to_tos(source_type, data)
        if not tos_url:
            return data, None
        filename: str | None = None
        if registry:
            filename = await registry.register(tos_url)
        return tos_url, filename
    except Exception:
        logger.warning("upload_and_register failed", exc_info=True)
        return data, None
