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
    """Decorator: route tool failures per contract §4.7/§4.8.

    Every failure surfaces to the LLM as a ``ToolOutcomeError`` dict —
    no exception escapes the wrapper. The agent turn stays alive even
    when a single tool call goes wrong; the LLM gets to decide whether
    to retry, change strategy, or tell the user the tool isn't working.

    Routing table:

    * ``CapabilityInvalidArg`` → ``ToolOutcomeError(kind="invalid_args")``
    * ``CapabilityNotFound``   → ``ToolOutcomeError(kind="not_found")``
    * any other ``Exception``  → ``ToolOutcomeError(kind="tool_error")``
      with ``detail["original_error_type"]`` set so the LLM can reason
      about it. Hotfix 2026-05-13 — previously this branch propagated
      and killed the whole agent turn (trace
      9b5a451cd00ccf735427cbb2059a95fb).

    ``BaseException`` subclasses (``CancelledError`` /
    ``KeyboardInterrupt`` / ``SystemExit``) deliberately do NOT get
    wrapped; they signal shutdown / cancellation and must travel up so
    the runtime can unwind cleanly.

    Success values pass through unchanged.
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
            except Exception as exc:
                # Catch-all to keep the agent turn alive. Log with
                # exc_info so the technical detail survives in
                # observability while the LLM gets a structured outcome.
                logger.warning(
                    "%s: tool failure — %s",
                    func.__name__,
                    exc,
                    exc_info=True,
                )
                message = (
                    f"{error_message}: {exc}" if error_message else str(exc)
                )
                return ToolOutcomeError(
                    kind="tool_error",
                    message=message,
                    detail={"original_error_type": type(exc).__name__},
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
