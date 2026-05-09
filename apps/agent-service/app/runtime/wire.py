"""wire() DSL: declarative producer-consumer connection language.

Business code calls ``wire(T).to(consumer).durable()...`` to describe
how a Data type flows through the graph. Each call appends a
``WireSpec`` to ``WIRING_REGISTRY``; later phases (compile_graph, emit,
durable, engine) consume the registry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.runtime.data import Data
from app.runtime.sink import SinkSpec
from app.runtime.source import SourceSpec

VALID_ON_ERROR: tuple[str, ...] = ("dlq", "ignore-duplicate", "manual-review")


@dataclass(frozen=True)
class RetryPolicy:
    """Retry configuration for a durable wire (Gap 7.3).

    ``n`` = maximum total attempts (first delivery + retries combined).
    ``backoff`` = "exponential" (base * 2^(attempt-1)) or "linear" (base * attempt).
    ``base_delay_ms`` / ``max_delay_ms`` clamp the per-attempt delay.
    ``lease_ms`` = how long the inflight ``processing`` row is reserved
    for this worker; another consumer may take over after expiry (lease
    semantics live in runtime/inflight.py — Task 4).
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


@dataclass
class WireSpec:
    data_type: type[Data]
    consumers: list[Callable] = field(default_factory=list)
    sinks: list[SinkSpec] = field(default_factory=list)
    sources: list[SourceSpec] = field(default_factory=list)
    durable: bool = False
    as_latest: bool = False
    predicate: Callable | None = None
    debounce: dict | None = None
    debounce_key_by: Callable[[Data], str] | None = None
    with_latest: tuple[type[Data], ...] = ()
    retry: RetryPolicy | None = None
    # str (not Literal) for forward-compat with future policies — matches
    # RetryPolicy.backoff. Validated at builder time via VALID_ON_ERROR.
    on_error: str = "dlq"


WIRING_REGISTRY: list[WireSpec] = []


def clear_wiring() -> None:
    WIRING_REGISTRY.clear()


class WireBuilder:
    def __init__(self, data_type: type[Data]):
        self._spec = WireSpec(data_type=data_type)
        WIRING_REGISTRY.append(self._spec)

    def to(self, *targets) -> WireBuilder:
        for t in targets:
            if isinstance(t, SinkSpec):
                self._spec.sinks.append(t)
            else:
                self._spec.consumers.append(t)
        return self

    def from_(self, *sources: SourceSpec) -> WireBuilder:
        self._spec.sources.extend(sources)
        return self

    def durable(self) -> WireBuilder:
        self._spec.durable = True
        return self

    def as_latest(self) -> WireBuilder:
        self._spec.as_latest = True
        return self

    def when(self, pred: Callable) -> WireBuilder:
        self._spec.predicate = pred
        return self

    def debounce(
        self,
        *,
        seconds: int,
        max_buffer: int,
        key_by: Callable[[Data], str],
    ) -> WireBuilder:
        """Declare debounce semantics on this wire.

        ``key_by`` extracts a partition key from each Data instance —
        debounce state (latest trigger_id, count) is per-key. Required
        (no default) so every debounce wire explicitly names its
        partition.
        """
        self._spec.debounce = {"seconds": seconds, "max_buffer": max_buffer}
        self._spec.debounce_key_by = key_by
        return self

    def with_latest(self, *types: type[Data]) -> WireBuilder:
        self._spec.with_latest = types
        return self

    def retry(
        self,
        *,
        n: int,
        backoff: str = "exponential",
        base_delay_ms: int = 500,
        max_delay_ms: int = 30_000,
        lease_ms: int = 300_000,
    ) -> WireBuilder:
        """Configure retry policy for a durable wire (Gap 7.3).

        Must be called after ``.durable()``. Without retry the wire
        keeps the legacy fail-to-DLQ semantic (handler exception nacks
        with requeue=False); with retry the handler republishes a
        delayed copy until ``n`` attempts are exhausted, then DLQs.

        ``lease_ms`` reserves the inflight ``processing`` row for this
        worker; another consumer may take over after expiry. See
        runtime/inflight.py for the state-machine semantics.
        """
        if not self._spec.durable:
            raise ValueError("retry() must come after .durable()")
        if n < 1:
            raise ValueError("retry n must be >= 1")
        if backoff not in ("exponential", "linear"):
            raise ValueError(
                f"backoff must be 'exponential' or 'linear', got {backoff!r}"
            )
        if lease_ms < 1:
            raise ValueError("lease_ms must be >= 1")
        self._spec.retry = RetryPolicy(
            n=n,
            backoff=backoff,
            base_delay_ms=base_delay_ms,
            max_delay_ms=max_delay_ms,
            lease_ms=lease_ms,
        )
        return self

    def on_error(self, policy: str) -> WireBuilder:
        """Configure error policy for this wire (Gap 18).

        Valid values: 'dlq' (default — fall to DLQ),
        'ignore-duplicate' (ack DuplicateData silently),
        'manual-review' (route NeedsReview to review queue).
        retry is controlled separately by .retry(); on_error decides
        what happens AFTER retries are exhausted or for non-retryable
        errors.
        """
        if policy not in VALID_ON_ERROR:
            raise ValueError(
                f"on_error policy must be one of {VALID_ON_ERROR}, got {policy!r}"
            )
        self._spec.on_error = policy
        return self


def wire(data_type: type[Data]) -> WireBuilder:
    return WireBuilder(data_type)
