"""Retry policy and the retry decision for a delivery that failed.

Pure decision logic — no I/O, no broker calls. Given inbound message
headers + a ``RetryPolicy``, returns a RetryDecision describing
whether to republish (with backoff delay) or dead-letter. The
messaging layer (``app.messaging.receiving``) is the caller.

Delivery count is read from the runtime-managed ``x-delivery-count``
header ONLY. We don't read RabbitMQ's automatic ``x-death`` header
because its accumulation semantics depend on broker DLX configuration
and are not the canonical attempt counter for this runtime — the
publisher-side retry path always sets x-delivery-count explicitly.

First delivery has no header (count == 0).

Idempotent_key reuse: retries are republished with the same body and
headers (apart from ``x-delivery-count``). The inflight state machine
(runtime/inflight.py) keys off the same idempotent_key, so retries flow
back into the same row (state=failed → processing on next claim) — they
don't bypass dedup, they continue it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

DELIVERY_COUNT_HEADER = "x-delivery-count"


def delivery_count(headers: dict[str, Any] | None) -> int:
    """Extract the runtime's delivery_count from inbound message headers.

    Defensive: any non-int / negative / missing value coerces to 0
    (treated as first delivery).
    """
    h = headers or {}
    v = h.get(DELIVERY_COUNT_HEADER)
    if isinstance(v, int) and v >= 0:
        return v
    return 0


@dataclass(frozen=True)
class RetryPolicy:
    """How often a failed delivery is tried again, and how long it waits.

    ``n`` = maximum total attempts (first delivery + retries combined).
    ``backoff`` = "exponential" (base * 2^(attempt-1)) or "linear" (base * attempt).
    ``base_delay_ms`` / ``max_delay_ms`` clamp the per-attempt delay.
    ``lease_ms`` = how long the inflight ``processing`` row is reserved
    for this worker; another consumer may take over after expiry (lease
    semantics live in runtime/inflight.py).
    """

    n: int
    backoff: str
    base_delay_ms: int
    max_delay_ms: int
    lease_ms: int

    def delay_for_attempt(self, attempt: int) -> int:
        """Calculate delay for the Nth attempt (1-indexed)."""
        if self.backoff == "linear":
            d = self.base_delay_ms * attempt
        else:  # exponential
            d = self.base_delay_ms * (2 ** (attempt - 1))
        return min(d, self.max_delay_ms)


@dataclass(frozen=True)
class RetryDecision:
    action: Literal["retry", "dlq"]
    attempt: int  # x-delivery-count value for the UPCOMING republish; 0 if dlq
    delay_ms: int


def decide_retry(
    *, headers: dict[str, Any] | None, policy: RetryPolicy | None
) -> RetryDecision:
    """Decide whether to retry with backoff or send to DLQ.

    Semantics: ``RetryPolicy.n`` is the max total attempts (first delivery
    + retries combined). Inbound ``x-delivery-count`` header counts how
    many times this message has been delivered so far (0 = first).
    Failure of the (count+1)-th attempt:

    - If count+1 == n → DLQ budget exhausted, no more retries.
    - If count+1 < n  → retry; publish a new copy with
      ``x-delivery-count = count+1`` and the matching backoff delay.

    No policy → always DLQ.
    """
    if policy is None:
        return RetryDecision(action="dlq", attempt=0, delay_ms=0)
    count = delivery_count(headers)
    if count + 1 >= policy.n:
        return RetryDecision(action="dlq", attempt=0, delay_ms=0)
    next_count = count + 1
    # next_count is 1-indexed retry sequence number (1 = first retry).
    # The base_delay matches the first retry: linear → base * 1, expo →
    # base * 2^0 = base; later retries scale up.
    delay = policy.delay_for_attempt(next_count)
    return RetryDecision(action="retry", attempt=next_count, delay_ms=delay)
