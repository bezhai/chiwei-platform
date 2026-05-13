"""Shared helpers for agent tools — error handling, image upload, metrics."""

from __future__ import annotations

import functools
import logging
from typing import Any

from prometheus_client import Counter, Histogram

from app.agent.tools.outcome import (
    ToolInvalidArgs,
    ToolNotFound,
    ToolOutcomeError,
)
from app.capabilities._errors import (
    CapabilityInvalidArg,
    CapabilityNotFound,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prometheus helpers
# ---------------------------------------------------------------------------

# prometheus_client has no public API to retrieve an already-registered
# collector by name. _names_to_collectors is the only option; tracked in
# https://github.com/prometheus/client_python/issues/546


def get_or_create_counter(name: str, doc: str, labels: list[str]) -> Counter:
    """Get an existing Counter or create a new one (safe for re-import)."""
    try:
        return Counter(name, doc, labels)
    except ValueError:
        from prometheus_client import REGISTRY

        return REGISTRY._names_to_collectors[name.removesuffix("_total")]  # type: ignore[return-value]


def get_or_create_histogram(
    name: str, doc: str, labels: list[str] | None = None
) -> Histogram:
    """Get an existing Histogram or create a new one (safe for re-import)."""
    try:
        return Histogram(name, doc, labels or [])
    except ValueError:
        from prometheus_client import REGISTRY

        return REGISTRY._names_to_collectors[name]  # type: ignore[return-value]


def tool_error(error_message: str):
    """Decorator: route tool failures per contract §4.7/§4.8 — C3.

    Routing table:

    * ``CapabilityInvalidArg``  → converted to ``ToolInvalidArgs`` and
      returned to the LLM as a ``ToolOutcomeError(kind="invalid_args")``
      dict. The LLM sees a structured outcome and can adjust the next call.
    * ``CapabilityNotFound``    → same path with ``kind="not_found"``.
    * ``CapabilityTimeout``     → propagated. Wire ``on_error`` decides
      DLQ / retry / manual-review (contract §1 forbids node-level on_error).
    * ``CapabilityRateLimited`` → propagated.
    * ``CapabilityCallFailed``  → propagated.
    * any other ``Exception``   → propagated. No silent swallow.

    The ``error_message`` argument is kept for call-site context (it
    prefixes the LLM-visible message); historical "friendly string" return
    is gone. Tools that return strings/dicts/lists on success are
    unaffected — success values pass through unchanged.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except CapabilityInvalidArg as exc:
                logger.warning(
                    "%s: invalid args — %s (meta=%s)",
                    func.__name__,
                    exc,
                    exc.meta,
                )
                typed = ToolInvalidArgs(str(exc), detail=exc.meta or None)
                return ToolOutcomeError(
                    kind="invalid_args",
                    message=f"{error_message}: {typed.message}"
                    if error_message
                    else typed.message,
                    detail=typed.detail or None,
                ).model_dump()
            except CapabilityNotFound as exc:
                logger.warning(
                    "%s: resource not found — %s (meta=%s)",
                    func.__name__,
                    exc,
                    exc.meta,
                )
                typed = ToolNotFound(str(exc), detail=exc.meta or None)
                return ToolOutcomeError(
                    kind="not_found",
                    message=f"{error_message}: {typed.message}"
                    if error_message
                    else typed.message,
                    detail=typed.detail or None,
                ).model_dump()

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
