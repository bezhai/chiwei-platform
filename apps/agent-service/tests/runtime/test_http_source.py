from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.runtime.data import Data, Key
from app.runtime.emit import reset_emit_runtime
from app.runtime.graph import compile_graph
from app.runtime.http_source import register_http_sources
from app.runtime.node import node
from app.runtime.source import Source
from app.runtime.wire import clear_wiring, wire


class Req(Data):
    rid: Annotated[str, Key]
    payload: dict


received: list[Req] = []


@node
async def http_handler(r: Req) -> None:
    received.append(r)


def setup_function():
    clear_wiring()
    received.clear()
    reset_emit_runtime()


def test_http_source_registers_endpoint_and_emits():
    wire(Req).from_(Source.http("/chat-test")).to(http_handler)
    compile_graph()

    app = FastAPI()
    register_http_sources(app)

    client = TestClient(app)
    r = client.post("/chat-test", json={"rid": "r1", "payload": {"x": 1}})

    assert r.status_code == 202
    assert r.json() == {"accepted": True}
    assert len(received) == 1
    assert received[0].rid == "r1"
    assert received[0].payload == {"x": 1}


def test_http_source_skips_non_http_sources():
    # A cron-source wire should NOT produce an HTTP endpoint.
    wire(Req).from_(Source.cron("* * * * *")).to(http_handler)
    compile_graph()

    app = FastAPI()
    register_http_sources(app)

    client = TestClient(app)
    r = client.post("/anything", json={"rid": "r1", "payload": {}})
    assert r.status_code == 404  # no route registered
