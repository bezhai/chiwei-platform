"""Sink specs: declarative descriptors for outbound edges of a wire.

A ``SinkSpec`` names an external consumer (feishu send, HTTP callback,
Langfuse trace, ...) that receives Data from the graph. Factories on
``Sink`` construct specs; the engine interprets ``kind`` to wire up the
actual adapter at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SinkSpec:
    kind: str
    params: dict = field(default_factory=dict)


class Sink:
    @staticmethod
    def feishu_send() -> SinkSpec:
        return SinkSpec("feishu_send")

    @staticmethod
    def http_callback(url: str) -> SinkSpec:
        return SinkSpec("http_callback", {"url": url})

    @staticmethod
    def langfuse_trace() -> SinkSpec:
        return SinkSpec("langfuse_trace")
