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

from fastapi import FastAPI, Request

from app.runtime.emit import emit
from app.runtime.http_auth import (
    refusal_detail,
    request_data,
    request_fields,
    route_guards,
)
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
    detail_for = refusal_detail(src.params.get("answers_with_lane", False))

    async def endpoint(req: Request, **path_kwargs: Any) -> Any:
        # Path params first, then the query string, then (POST / PUT) the JSON body.
        fields = {**path_kwargs, **await request_fields(req, method)}
        data_obj = request_data(data_cls, fields, detail_for)

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

    # 凭据、泳道校验挂成**路由级依赖**，只挂在声明了的那几条上，跑在参数反序列化之前
    # （见 :mod:`app.runtime.http_auth`）。
    guard = route_guards(
        inner_secret=src.params.get("requires_inner_secret", False),
        lane_match=src.params.get("requires_lane_match", False),
        detail_for=detail_for,
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
