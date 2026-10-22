"""Prometheus collectors that are created once per process and found again on re-import.

A collector registers itself under its name when it is built; building the same name a second time
(a module imported twice, a test that reloads it) raises ``ValueError``. These two build it the first
time and hand back the registered one after that.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

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
