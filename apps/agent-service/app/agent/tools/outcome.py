"""Tool outcome contract — C3.

When an ``@tool`` function fails with a *business-semantic* error (the
caller asked for the wrong thing — bad arg, missing resource), the LLM
should *see* a structured outcome so it can adjust the next call. This
module defines:

* :class:`ToolOutcomeError` — pydantic model serialized into the
  ``ToolMessage`` content LLM sees.
* :class:`ToolInvalidArgs` / :class:`ToolNotFound` — typed exceptions
  that ``@tool_error`` (see ``_common.py``) raises internally before
  converting to the outcome dict.

Failures that are *not* business-semantic (timeout / rate-limit /
upstream 5xx) propagate out of ``@tool_error`` and are handled by wire
``on_error`` policies (contract §1 forbids node-level on_error).

Contract reference: ``docs/guides/dataflow-node-contract.md`` §4.7 + §4.8.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


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


__all__ = [
    "ToolInvalidArgs",
    "ToolNotFound",
    "ToolOutcomeError",
]
