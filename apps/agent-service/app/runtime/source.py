"""Source specs: declarative descriptors for inbound edges of a wire.

A ``SourceSpec`` names an external producer that feeds Data into the
graph. Factories on ``Source`` construct specs; the engine interprets
``kind`` to wire up the actual adapter at runtime.

Surface kept intentionally minimal — every kind here has a real
adapter wired up in the engine. Business-specific entry points
(feishu webhooks, ops-manual triggers, ...) live in their own services
(channel-server webhook ingress, /ops endpoints) and feed the graph through ``Source.mq``
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
    def http(
        path: str,
        *,
        method: str = "POST",
        response: bool = False,
    ) -> SourceSpec:
        """HTTP source.

        method: "GET" | "POST" | "PUT" | "DELETE". path 中 ``{name}`` 占位的
        部分自动绑定为 path param，按字段名注入到 Data 实例。
        GET / DELETE 把 query string 反序列化进 Data。
        POST / PUT 默认 body JSON 反序列化进 Data。

        response=True 表示节点返回值会作为 HTTP response body 同步返回；
        runtime 会在 emit 后等节点完成（in-process consumer 必须在本进程，
        跨进程的 RPC 模式 v4 不支持，会在编译期 raise）。
        """
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "DELETE"}:
            raise ValueError(f"unsupported HTTP method {method!r}")
        return SourceSpec("http", {"path": path, "method": method, "response": response})

    @staticmethod
    def cron(expr: str, *, tz: str = "UTC") -> SourceSpec:
        """5-field cron expression. ``tz``: IANA zone name
        (e.g. 'Asia/Shanghai'); the loop fires at the right wall-clock
        time in that zone. ``croniter.get_next`` is absolute-time based.
        """
        return SourceSpec("cron", {"expr": expr, "tz": tz})

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
