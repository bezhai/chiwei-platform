"""Register HTTP-kind sources as FastAPI endpoints.

For each wire declaring ``.from_(Source.http(path))``, we bind a POST route
at ``path`` that deserializes the JSON body into the wire's ``Data`` type
and emits it. Response is ``202 Accepted`` — emit is fire-and-forget from
the caller's point of view (the graph decides whether the consumer is
in-process or durable).
"""

from __future__ import annotations

from fastapi import FastAPI, Request

from app.runtime.emit import emit
from app.runtime.wire import WIRING_REGISTRY


def register_http_sources(app: FastAPI) -> None:
    """Attach a POST route for every ``Source.http(path)`` source in the registry.

    Call this after all wiring modules have been imported, before serving
    traffic.
    """
    for w in WIRING_REGISTRY:
        for src in w.sources:
            if src.kind != "http":
                continue
            path = src.params["path"]
            data_cls = w.data_type

            async def endpoint(req: Request, cls=data_cls) -> dict:
                body = await req.json()
                await emit(cls(**body))
                return {"accepted": True}

            app.post(path, status_code=202)(endpoint)
