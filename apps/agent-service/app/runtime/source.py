"""Source specs: declarative descriptors for inbound edges of a wire.

A ``SourceSpec`` names an external producer that feeds Data into the
graph. Factories on ``Source`` construct specs; the engine interprets
``kind`` to wire up the actual adapter at runtime.

Surface kept intentionally minimal — every kind here has a real
adapter wired up in the engine. Business-specific entry points
(feishu webhooks, ops-manual triggers, ...) live in their own services
(lark-proxy, /ops endpoints) and feed the graph through ``Source.mq``
or a plain ``Source.http`` route.
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
    def interval(seconds: float) -> SourceSpec:
        """Simple periodic source: emit every ``seconds`` seconds.

        Cron expressions have a 1-minute minimum resolution (standard
        5-field format); ``interval`` fills the sub-minute niche and also
        gives tests a fast-firing source without mocking croniter.
        """
        if seconds <= 0:
            raise ValueError(f"Source.interval(seconds={seconds!r}) must be positive")
        return SourceSpec("interval", {"seconds": float(seconds)})

    @staticmethod
    def mq(queue: str) -> SourceSpec:
        return SourceSpec("mq", {"queue": queue})
