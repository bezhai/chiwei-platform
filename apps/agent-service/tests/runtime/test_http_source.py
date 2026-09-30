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


def test_http_source_post_with_query_string():
    """POST endpoints should accept query-string params (FastAPI legacy contract)."""
    captured: list = []

    @node
    async def handler(p: _Ping) -> None:
        captured.append(p)

    wire(_Ping).from_(Source.http("/post-q", method="POST")).to(handler)

    app = FastAPI()
    register_http_sources(app)
    client = TestClient(app)

    # POST with no body, only query
    r = client.post("/post-q?name=zoe")
    assert r.status_code == 202
    assert captured[-1].name == "zoe"


def test_http_source_post_body_overrides_query():
    """When both body and query supply the same field, body wins."""
    captured: list = []

    @node
    async def handler(p: _Ping) -> None:
        captured.append(p)

    wire(_Ping).from_(Source.http("/post-bq", method="POST")).to(handler)

    app = FastAPI()
    register_http_sources(app)
    client = TestClient(app)

    r = client.post("/post-bq?name=fromquery", json={"name": "frombody"})
    assert r.status_code == 202
    assert captured[-1].name == "frombody"


# ---------------------------------------------------------------------------
# requires_lane_match：请求要去的泳道和进程所在的泳道不一致时，一步都不做
# ---------------------------------------------------------------------------


def _lane_bound_app(monkeypatch, *, process_lane: str | None, answers_with_lane=True):
    """一条声明了 requires_lane_match 的路由；返回 (client, 被调用的记录)。"""
    if process_lane is None:
        monkeypatch.delenv("LANE", raising=False)
    else:
        monkeypatch.setenv("LANE", process_lane)
    seen: list = []

    @node
    async def lane_bound(p: _Ping) -> _Pong:
        seen.append(p)
        return _Pong(name=p.name)

    wire(_Ping).from_(
        Source.http(
            "/lane-bound",
            method="POST",
            response=True,
            requires_lane_match=True,
            answers_with_lane=answers_with_lane,
        )
    ).to(lane_bound)
    app = FastAPI()
    register_http_sources(app)
    return TestClient(app), seen


def test_a_request_for_another_lane_is_refused_before_the_handler(monkeypatch):
    client, seen = _lane_bound_app(monkeypatch, process_lane="coe-here")

    r = client.post("/lane-bound", json={"name": "x"}, headers={"x-ctx-lane": "coe-there"})

    assert r.status_code == 409
    assert r.json()["detail"]["lane"] == "coe-here"
    assert "coe-there" in r.json()["detail"]["message"]
    assert seen == []


def test_a_request_without_a_lane_is_meant_for_prod(monkeypatch):
    client, seen = _lane_bound_app(monkeypatch, process_lane="coe-here")

    r = client.post("/lane-bound", json={"name": "x"})

    assert r.status_code == 409
    assert seen == []


def test_a_request_for_this_lane_goes_through(monkeypatch):
    client, seen = _lane_bound_app(monkeypatch, process_lane="coe-here")

    r = client.post("/lane-bound", json={"name": "x"}, headers={"x-ctx-lane": "coe-here"})

    assert r.status_code == 200
    assert r.json() == {"name": "x"}
    assert len(seen) == 1


@pytest.mark.parametrize("header", [None, "prod"])
def test_prod_takes_requests_without_a_lane_or_for_prod(monkeypatch, header):
    client, seen = _lane_bound_app(monkeypatch, process_lane=None)

    headers = {"x-ctx-lane": header} if header else {}
    r = client.post("/lane-bound", json={"name": "x"}, headers=headers)

    assert r.status_code == 200
    assert len(seen) == 1


def test_the_lane_is_checked_before_the_parameters_are_read(monkeypatch):
    """落错泳道的请求不该拿到 422：那等于把参数结构告诉了一个本不该到这里的请求。"""
    client, seen = _lane_bound_app(monkeypatch, process_lane="coe-here")

    r = client.post("/lane-bound", json={"wrong": 1}, headers={"x-ctx-lane": "coe-there"})

    assert r.status_code == 409
    assert seen == []


def test_a_route_that_does_not_report_its_lane_refuses_with_a_sentence(monkeypatch):
    client, _ = _lane_bound_app(monkeypatch, process_lane="coe-here", answers_with_lane=False)

    r = client.post("/lane-bound", json={"name": "x"}, headers={"x-ctx-lane": "coe-there"})

    assert r.status_code == 409
    assert isinstance(r.json()["detail"], str)


def test_routes_without_the_declaration_are_not_checked(monkeypatch):
    monkeypatch.setenv("LANE", "coe-here")
    seen: list = []

    @node
    async def open_route(p: _Ping) -> None:
        seen.append(p)

    wire(_Ping).from_(Source.http("/open", method="POST")).to(open_route)
    app = FastAPI()
    register_http_sources(app)

    r = TestClient(app).post("/open", json={"name": "x"}, headers={"x-ctx-lane": "coe-there"})

    assert r.status_code == 202
    assert len(seen) == 1
