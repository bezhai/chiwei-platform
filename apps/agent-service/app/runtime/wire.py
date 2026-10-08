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


def wire(data_type: type[Data]) -> WireBuilder:
    return WireBuilder(data_type)
