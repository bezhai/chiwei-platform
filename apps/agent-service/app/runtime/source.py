"""Source specs: declarative descriptors for inbound edges of a wire.

A ``SourceSpec`` names an external producer (HTTP endpoint, cron trigger,
MQ queue, ...) that feeds Data into the graph. Factories on ``Source``
construct specs; the engine interprets ``kind`` to wire up the actual
adapter at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceSpec:
    kind: str
    params: dict = field(default_factory=dict)


class Source:
    @staticmethod
    def http(path: str) -> SourceSpec:
        return SourceSpec("http", {"path": path})

    @staticmethod
    def cron(expr: str) -> SourceSpec:
        return SourceSpec("cron", {"expr": expr})

    @staticmethod
    def mq(queue: str) -> SourceSpec:
        return SourceSpec("mq", {"queue": queue})

    @staticmethod
    def feishu_webhook() -> SourceSpec:
        return SourceSpec("feishu_webhook")

    @staticmethod
    def manual(path: str) -> SourceSpec:
        return SourceSpec("manual", {"path": path})
