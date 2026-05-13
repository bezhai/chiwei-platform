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

from fastapi import FastAPI, HTTPException, Request

from app.runtime.emit import emit
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
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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

    status_code = 200 if sync_response else 202
    if method == "GET":
        app.get(path, status_code=status_code)(endpoint)
    elif method == "POST":
        app.post(path, status_code=status_code)(endpoint)
    elif method == "PUT":
        app.put(path, status_code=status_code)(endpoint)
    elif method == "DELETE":
        app.delete(path, status_code=status_code)(endpoint)
    else:
        raise ValueError(f"unsupported HTTP method {method!r}")


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
