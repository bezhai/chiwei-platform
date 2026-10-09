"""The dataflow edges of this process: ``WireSpec``s in ``WIRING_REGISTRY``.

The plugin host appends the edges its plugins register (``ctx.durable``,
``ctx.outbound``, :mod:`app.host.host`); ``wire(T).to(consumer).durable()``
builds the same spec directly. compile_graph, emit and durable read the
registry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.runtime.data import Data
from app.runtime.sink import SinkSpec


@dataclass
class WireSpec:
    data_type: type[Data]
    consumers: list[Callable] = field(default_factory=list)
    sinks: list[SinkSpec] = field(default_factory=list)
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

    def durable(self) -> WireBuilder:
        self._spec.durable = True
        return self


def wire(data_type: type[Data]) -> WireBuilder:
    return WireBuilder(data_type)
