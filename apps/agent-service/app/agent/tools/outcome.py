"""Tool outcome contract — C3.

When an ``@tool`` function fails with a *business-semantic* error (the
caller asked for the wrong thing — bad arg, missing resource), the LLM
should *see* a structured outcome so it can adjust the next call. This
module defines:

* :class:`ToolOutcomeError` — pydantic model serialized into the
  ``ToolMessage`` content LLM sees.
* :class:`ToolInvalidArgs` / :class:`ToolNotFound` — typed exceptions
  that :func:`tool_error` raises internally before converting to the
  outcome dict.
* :func:`tool_error` — the decorator every tool wears: it turns a tool's
  failure into a :class:`ToolOutcomeError` dict the LLM sees.

Failures that are *not* business-semantic (timeout / rate-limit /
upstream 5xx) propagate out of ``@tool_error`` to the caller.

The rules are described in ``docs/guides/plugin-host.md`` (section 6, tools and failures).
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Literal

from pydantic import BaseModel

from app.capabilities.errors import CapabilityInvalidArg, CapabilityNotFound

logger = logging.getLogger(__name__)


class ToolOutcomeError(BaseModel):
    """Structured tool failure visible to the LLM.

    Three kinds:

    * ``invalid_args`` — caller passed bad arguments (typed
      ``CapabilityInvalidArg``). LLM should adjust the next call.
    * ``not_found``    — target resource doesn't exist (typed
      ``CapabilityNotFound``). LLM should change strategy.
    * ``tool_error``   — anything else (timeout / 4xx / 5xx / unwrapped
      upstream error). LLM can retry, swap tools, or tell the user the
      tool isn't working. The decorator hands this back instead of
      propagating so the agent turn stays alive — losing a tool call
      shouldn't kill the whole reply (see trace
      9b5a451cd00ccf735427cbb2059a95fb).

    ``detail`` carries optional structured context (``param`` for
    invalid_args, ``resource_id`` for not_found, ``original_error_type``
    for tool_error). The LLM treats it as best-effort hints.
    """

    kind: Literal["invalid_args", "not_found", "tool_error"]
    message: str
    detail: dict[str, Any] | None = None


class ToolInvalidArgs(Exception):
    """Tool invoked with bad arguments — LLM should adjust next call.

    Raised by tool layer (and ``@tool_error``) after catching the
    underlying ``CapabilityInvalidArg``. The decorator converts it to a
    ``ToolOutcomeError`` dict before returning to LangGraph.
    """

    def __init__(
        self,
        message: str,
        *,
        param: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.param = param
        self.detail = dict(detail) if detail else {}


class ToolNotFound(Exception):
    """Tool target resource does not exist — LLM should adjust next call.

    Raised by tool layer (and ``@tool_error``) after catching the
    underlying ``CapabilityNotFound``.
    """

    def __init__(
        self,
        message: str,
        *,
        resource_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.resource_id = resource_id
        self.detail = dict(detail) if detail else {}


def tool_error(error_message: str):
    """Decorator: route tool failures (``docs/guides/plugin-host.md``, section 6).

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


__all__ = [
    "ToolInvalidArgs",
    "ToolNotFound",
    "ToolOutcomeError",
    "tool_error",
]


# What her agents and world's answer a tool call whose arguments do not fit its tool with
# (``AgentSpec.bad_arguments``: the tool's name and the binding error are filled in). The loop
# feeds it back so the model fixes the arguments and calls again in the same run; the wording is
# theirs, the generic dispatch only fills it in.
BAD_ARGUMENTS = "工具 {tool} 参数绑定失败：{error}。请检查该工具的参数定义，修正参数后重新调用。"
