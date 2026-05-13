"""Capability exception hierarchy — contract §4.8 / plan A3.

Five typed exception classes plus a common base. Capability and infra
modules raise these instead of returning ``False`` / ``None`` /
stringifying upstream failures; business nodes propagate them so wire
``on_error`` decides DLQ / review / swallow, and tool wrappers (see
``agent/tools/_common.py``, C3 territory) map a subset to LLM-visible
typed outcomes.

Routing table (contract §4.7 + §4.8):

* ``CapabilityInvalidArg``  — wrong arg / validation failure (LLM-visible).
* ``CapabilityNotFound``    — resource missing (LLM-visible).
* ``CapabilityTimeout``     — upstream timeout (retry-eligible).
* ``CapabilityRateLimited`` — upstream 429 / quota (retry w/ longer backoff).
* ``CapabilityCallFailed``  — upstream 5xx / protocol / refused (retry).

Every class supports ``message`` plus optional keyword-only ``meta``
dict for structured context that langfuse / logging can attach without
forcing the message string to carry every field.
"""
from __future__ import annotations

from typing import Any


class CapabilityError(Exception):
    """Base class for typed capability failures.

    ``meta`` is keyword-only so the positional slot stays a plain string —
    accidental ``CapabilityError("msg", {"k": v})`` is a TypeError, which
    surfaces miswiring before it can hide structured context inside the
    message string.
    """

    def __init__(self, message: str, *, meta: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.meta: dict[str, Any] = dict(meta) if meta else {}

    def __str__(self) -> str:  # explicit — Exception.__str__ already does this
        return self.message


class CapabilityInvalidArg(CapabilityError):
    """Caller passed bad arguments (schema/business validation failed)."""


class CapabilityNotFound(CapabilityError):
    """Requested resource does not exist (user/persona/file/record)."""


class CapabilityTimeout(CapabilityError):
    """Upstream timed out (HTTP / DB / LLM call)."""


class CapabilityRateLimited(CapabilityError):
    """Upstream signalled rate-limit (429 / quota exceeded / business limit)."""


class CapabilityCallFailed(CapabilityError):
    """Upstream failed for any other reason (5xx / protocol error / refused)."""


__all__ = [
    "CapabilityError",
    "CapabilityInvalidArg",
    "CapabilityNotFound",
    "CapabilityTimeout",
    "CapabilityRateLimited",
    "CapabilityCallFailed",
]
