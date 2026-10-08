"""Putting a plugin's admin route on the FastAPI app, and taking it off again.

The request becomes the route's Data (query string, then the JSON body on top for POST / PUT);
the handler's answer, a Data, is the 200 body. The credential and lane checks, the 422 and the
refusal shape are :mod:`app.runtime.http_auth`, shared with the dataflow HTTP sources. Compared
with those sources, path parameters and the 202 fire-and-forget mode are gone: no route uses them.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.routing import APIRoute

from app.runtime.data import Data
from app.runtime.http_auth import (
    refusal_detail,
    request_data,
    request_fields,
    route_guards,
)

METHODS = ("GET", "POST", "PUT", "DELETE")

Handler = Callable[[Data], Awaitable[Any]]


@dataclass(frozen=True)
class RouteSpec:
    method: str
    path: str
    request: type[Data]
    handler: Handler
    inner_secret: bool = False
    lane_match: bool = False
    answers_with_lane: bool = False


def bind_route(app: FastAPI, spec: RouteSpec) -> APIRoute:
    """Add the route to ``app`` and return it, for :func:`unbind_route`."""
    detail_for = refusal_detail(spec.answers_with_lane)

    async def endpoint(req: Request):
        data = request_data(spec.request, await request_fields(req, spec.method), detail_for)
        result = await spec.handler(data)
        if hasattr(result, "model_dump"):
            return result.model_dump()
        return result

    app.add_api_route(
        spec.path,
        endpoint,
        methods=[spec.method],
        status_code=200,
        dependencies=route_guards(
            inner_secret=spec.inner_secret,
            lane_match=spec.lane_match,
            detail_for=detail_for,
        ),
    )
    return app.router.routes[-1]


def unbind_route(app: FastAPI, route: APIRoute) -> None:
    """Take the route off ``app``; drop the cached OpenAPI document that still lists it."""
    app.router.routes.remove(route)
    app.openapi_schema = None
