"""Register HTTP-kind sources as FastAPI endpoints.

For each wire declaring ``.from_(Source.http(path, method=..., response=...))``,
we bind a route at ``path`` of the given HTTP method. Body / query / path params
are deserialized into the wire's ``Data`` type and emitted.

- method=POST/PUT: JSON body -> Data fields
- method=GET/DELETE: query string -> Data fields
- path "/x/{name}": path param ``name`` -> Data field ``name``
- response=True: node return value (a Data) is JSON-serialized as response body,
  status 200; emit awaits the consumer. Only valid when consumer is in-process.
- response=False (default): emit fire-and-forget, status 202.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request

from app.runtime.emit import emit
from app.runtime.http_auth import inner_secret_guard
from app.runtime.lane_policy import current_deployment_lane
from app.runtime.wire import WIRING_REGISTRY

_PATH_PARAM_RE = re.compile(r"\{([^}]+)\}")


def _path_params(path: str) -> list[str]:
    return _PATH_PARAM_RE.findall(path)


def register_http_sources(app: FastAPI) -> None:
    """Attach a route per ``Source.http(...)`` source in WIRING_REGISTRY."""
    for w in WIRING_REGISTRY:
        for src in w.sources:
            if src.kind != "http":
                continue
            _bind_one(app, w, src)


def _bind_one(app: FastAPI, w, src) -> None:
    path = src.params["path"]
    method = src.params.get("method", "POST").upper()
    sync_response = src.params.get("response", False)
    data_cls = w.data_type
    path_params = _path_params(path)
    answers_with_lane = src.params.get("answers_with_lane", False)

    def refusal_detail(message: str) -> Any:
        """框架在 handler 之外挡回去的那几种回答（401 / 503 / 422）长什么样。

        没声明 ``answers_with_lane`` 的路由拿到的还是原来那句话本身，**一个字节都没
        变** —— 那几条运维口今天就是这样答的。

        声明了的路由多一个执行泳道。它读的是本进程的部署环境，回显不了请求里的任何
        东西：泳道不在注册表里时请求会静默落到 prod 的 pod 上并返回一个正常的回答，
        自报的落点是"这次调用打的是我以为的那棵树"唯一的证据，而被拒的时候调用方同样
        需要这个答案。形状跟 handler 自己那几种拒绝一致（``lane`` + ``message``），
        免得同一条路由的两类拒绝长成两种东西。
        """
        if not answers_with_lane:
            return message
        return {"lane": current_deployment_lane() or "prod", "message": message}

    async def endpoint(req: Request, **path_kwargs: Any) -> Any:
        kwargs: dict[str, Any] = dict(path_kwargs)
        # Always merge query string — works for POST/PUT/GET/DELETE.
        kwargs.update(dict(req.query_params))
        if method in {"POST", "PUT"}:
            try:
                body = await req.json()
            except Exception:
                # Classification: HARMLESS per-request fallback. Empty / missing
                # / non-JSON body is treated as "no body fields"; query string
                # alone may still satisfy the Data class. Validation fails below
                # (data_cls(**kwargs)) → returns HTTP 422 to the caller.
                body = {}
            if isinstance(body, dict):
                # Body wins on conflict: explicit body fields take precedence
                # over implicit query string for endpoints that take both.
                kwargs.update(body)

        try:
            data_obj = data_cls(**kwargs)
        except Exception as exc:
            # Classification: PER-REQUEST validation failure → caller's
            # responsibility. Returns 422 to the HTTP caller; loop semantics
            # (contract §4.1) don't apply—HTTP source is a request/response
            # endpoint, not a polling loop.
            raise HTTPException(
                status_code=422, detail=refusal_detail(str(exc))
            ) from exc

        if not sync_response:
            await emit(data_obj)
            return {"accepted": True}

        result = await _emit_rpc(w, data_obj)
        if hasattr(result, "model_dump"):
            return result.model_dump()
        return result

    # FastAPI inspects ``endpoint.__signature__`` to derive its dependency
    # graph: anything that's not a path-param name shows up as a body /
    # query model. ``**path_kwargs`` would be misinterpreted as a body
    # field, so we always rewrite the signature to expose only ``req``
    # plus any ``{name}`` path placeholders.
    from inspect import Parameter, Signature

    params = [
        Parameter("req", Parameter.POSITIONAL_OR_KEYWORD, annotation=Request),
    ] + [
        Parameter(name, Parameter.POSITIONAL_OR_KEYWORD, annotation=str)
        for name in path_params
    ]
    endpoint.__signature__ = Signature(params)  # type: ignore[attr-defined]

    # 凭据校验挂成**路由级依赖**，只挂在声明了 requires_inner_secret 的那几条上。
    #
    # 两件事靠这个挂法成立：
    #   * 覆盖范围在结构上限死 —— 没声明的路由（/health、那几条运维口）连这段代码都
    #     走不到，不需要任何路径白名单来"记得别挡它们"；
    #   * 校验跑在参数反序列化**之前** —— FastAPI 先解依赖再调 handler，而参数是在
    #     handler 体内 data_cls(**kwargs) 才解的。反过来的话，没凭据的人能拿 422 的
    #     内容把参数结构探出来。
    guard = (
        [Depends(inner_secret_guard(refusal_detail))]
        if src.params.get("requires_inner_secret")
        else []
    )

    status_code = 200 if sync_response else 202
    bind = {
        "GET": app.get,
        "POST": app.post,
        "PUT": app.put,
        "DELETE": app.delete,
    }.get(method)
    if bind is None:
        raise ValueError(f"unsupported HTTP method {method!r}")
    bind(path, status_code=status_code, dependencies=guard)(endpoint)


async def _emit_rpc(w, data_obj):
    """RPC: only in-process consumer is supported. Return consumer's return value.

    For a single in-process consumer, run it directly so we can capture the
    return value (regular emit() drops returns). If the wire has multiple
    consumers, raise — RPC needs a single result.
    """
    if w.durable:
        raise RuntimeError(
            "Source.http(response=True) cannot be combined with .durable() — "
            "need single in-process consumer to capture return value"
        )
    consumers = list(w.consumers)
    if len(consumers) != 1:
        raise RuntimeError(
            f"Source.http(response=True) requires exactly 1 consumer, "
            f"got {len(consumers)} on {data_obj.__class__.__name__}"
        )
    return await consumers[0](data_obj)
