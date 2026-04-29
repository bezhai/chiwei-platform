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
    debounce_key_by: Callable | None = None  # debounce wire 的 partition key 提取函数
    with_latest: tuple[type[Data], ...] = ()


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


def wire(data_type: type[Data]) -> WireBuilder:
    return WireBuilder(data_type)
