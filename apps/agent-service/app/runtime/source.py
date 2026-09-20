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
        requires_inner_secret: bool = False,
        answers_with_lane: bool = False,
    ) -> SourceSpec:
        """HTTP source.

        method: "GET" | "POST" | "PUT" | "DELETE". path 中 ``{name}`` 占位的
        部分自动绑定为 path param，按字段名注入到 Data 实例。
        GET / DELETE 把 query string 反序列化进 Data。
        POST / PUT 默认 body JSON 反序列化进 Data。

        response=True 表示节点返回值会作为 HTTP response body 同步返回；
        runtime 会在 emit 后等节点完成（in-process consumer 必须在本进程，
        跨进程的 RPC 模式 v4 不支持，会在编译期 raise）。

        requires_inner_secret=True 表示这条路由要校验 ``INNER_HTTP_SECRET``
        （``Authorization: Bearer``，见 :mod:`app.runtime.http_auth`）：没带、带错、
        进程没配这把凭据，三种都进不到 handler。

        **声明在这里而不是挂全局中间件**，是为了让"挡哪几条"在结构上成立：这个服务
        的路由是 ``register_http_sources`` 自动注册的，挂中间件会顺手盖住 ``/health``
        和那几条运维口，而靠路径前缀去认又把"哪些该挡"变成两处各写一遍的东西。默认
        ``False`` 是因为现有那几条运维口今天就是裸的，这次要加的是那四条文档树端点的
        门，不是顺手把整段前缀关起来。

        answers_with_lane=True 表示这条路由**每一种**回答都带上执行它的那个进程自己的
        泳道。handler 自己返回的那几种由 handler 负责；这个开关管的是框架在 handler
        之外挡回去的那几种（凭据不对的 401 / 503、参数反序列化失败的 422）——它们原来
        只有一句话，调用方于是恰恰在被拒的时候不知道是哪个进程拒的。

        它跟 ``requires_inner_secret`` 是两件事，所以是两个开关：一条路由可以要凭据而
        不自报落点，也可以反过来。合成一个的话，下一条要凭据的路由会跟着把自己的部署
        身份告诉没通过校验的人，而那不是任何人选过的。
        """
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "DELETE"}:
            raise ValueError(f"unsupported HTTP method {method!r}")
        return SourceSpec(
            "http",
            {
                "path": path,
                "method": method,
                "response": response,
                "requires_inner_secret": requires_inner_secret,
                "answers_with_lane": answers_with_lane,
            },
        )

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
