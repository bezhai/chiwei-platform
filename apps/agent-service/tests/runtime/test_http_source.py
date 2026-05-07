"""Phase 6 v4 Gap 1: http_source 扩 method / path_params / query / RPC."""
from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.runtime import Data, Key, Source, node, wire
from app.runtime.emit import reset_emit_runtime
from app.runtime.http_source import register_http_sources
from app.runtime.placement import clear_bindings
from app.runtime.wire import clear_wiring


@pytest.fixture(autouse=True)
def _isolate():
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    yield
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()


class _Ping(Data):
    name: Annotated[str, Key]

    class Meta:
        transient = True


class _Pong(Data):
    name: Annotated[str, Key]

    class Meta:
        transient = True


def test_http_source_get_with_query_param():
    """Source.http(method=GET) 把 query string 注入 Data。"""
    captured: list = []

    @node
    async def handler(p: _Ping) -> None:
        captured.append(p)

    wire(_Ping).from_(Source.http("/ping", method="GET")).to(handler)

    app = FastAPI()
    register_http_sources(app)
    client = TestClient(app)

    r = client.get("/ping?name=zoe")
    assert r.status_code == 202
    assert len(captured) == 1
    assert captured[0].name == "zoe"


def test_http_source_delete_with_path_param():
    """path 中 {name} 占位绑定 path param。"""
    captured: list = []

    @node
    async def handler(p: _Ping) -> None:
        captured.append(p)

    wire(_Ping).from_(Source.http("/items/{name}", method="DELETE")).to(handler)

    app = FastAPI()
    register_http_sources(app)
    client = TestClient(app)

    r = client.delete("/items/x")
    assert r.status_code == 202
    assert captured[0].name == "x"


def test_http_source_rpc_response_body():
    """response=True 时 node 返回值作为 HTTP response body 同步返回。"""

    @node
    async def handler(p: _Ping) -> _Pong:
        return _Pong(name=p.name + "_pong")

    wire(_Ping).from_(Source.http("/rpc", method="POST", response=True)).to(handler)

    app = FastAPI()
    register_http_sources(app)
    client = TestClient(app)

    r = client.post("/rpc", json={"name": "ping"})
    assert r.status_code == 200
    assert r.json() == {"name": "ping_pong"}


def test_http_source_post_default_unchanged():
    """method 默认 POST + JSON body 行为跟原 36 行 http_source 等价。"""
    captured: list = []

    @node
    async def handler(p: _Ping) -> None:
        captured.append(p)

    wire(_Ping).from_(Source.http("/legacy")).to(handler)

    app = FastAPI()
    register_http_sources(app)
    client = TestClient(app)

    r = client.post("/legacy", json={"name": "old"})
    assert r.status_code == 202
    assert captured[0].name == "old"


def test_http_source_skips_non_http_sources():
    """A cron-source wire should NOT produce an HTTP endpoint."""

    @node
    async def handler(p: _Ping) -> None:
        pass

    wire(_Ping).from_(Source.cron("* * * * *")).to(handler)

    app = FastAPI()
    register_http_sources(app)
    client = TestClient(app)

    r = client.post("/anything", json={"name": "x"})
    assert r.status_code == 404
