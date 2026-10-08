"""Admin routes: the request becomes the route's Data, the handler's answer is the body; the inner
credential, then the lane, are checked before the parameters are read; refusals carry the lane
when the route says so; stop takes the route back off the app.

The lane-match tests are ported from ``tests/runtime/test_http_source.py``: the guard they pin
now lives in :mod:`app.runtime.http_auth` and is shared by the dataflow HTTP sources and the host.
"""
from __future__ import annotations

import dataclasses
from typing import Annotated

import httpx
import pytest
from fastapi import FastAPI

from app.host import Host
from app.infra import config
from app.runtime import Data, Key

from .conftest import plugin

SECRET = "host-route-secret"
BASE = "http://host"


class _Ping(Data):
    name: Annotated[str, Key]

    class Meta:
        transient = True


class _Pong(Data):
    name: Annotated[str, Key]

    class Meta:
        transient = True


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, inner_http_secret=SECRET)
    )


class _Served:
    """A host serving one route on a fresh app; ``seen`` is every request the handler got."""

    def __init__(self, method: str, path: str, **flags) -> None:
        self.seen: list[_Ping] = []
        self.app = FastAPI()

        async def pong(p: _Ping) -> _Pong:
            self.seen.append(p)
            return _Pong(name=p.name)

        self.host = Host(
            "agent-service",
            [plugin("p", lambda ctx: ctx.route(method, path, _Ping, pong, **flags))],
        )

    def client(self, **headers) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=BASE, headers=headers
        )


@pytest.fixture
async def serve():
    served: list[_Served] = []

    async def start(method: str, path: str, **flags) -> _Served:
        s = _Served(method, path, **flags)
        await s.host.start(http=s.app, schema=False, mq=False, clocks=False, tasks=False)
        served.append(s)
        return s

    yield start
    for s in served:
        await s.host.stop()


def _auth(lane: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {SECRET}"}
    if lane is not None:
        headers["x-ctx-lane"] = lane
    return headers


# ---------------------------------------------------------------------------
# the request and the answer
# ---------------------------------------------------------------------------


async def test_the_json_body_becomes_the_data_and_the_answer_is_the_body(serve):
    s = await serve("POST", "/rpc")
    async with s.client() as c:
        r = await c.post("/rpc", json={"name": "ping"})

    assert r.status_code == 200
    assert r.json() == {"name": "ping"}
    assert s.seen == [_Ping(name="ping")]


async def test_get_reads_the_query_string(serve):
    s = await serve("GET", "/ping")
    async with s.client() as c:
        r = await c.get("/ping?name=zoe")

    assert r.status_code == 200
    assert s.seen == [_Ping(name="zoe")]


async def test_post_reads_the_query_string_too(serve):
    s = await serve("POST", "/post-q")
    async with s.client() as c:
        r = await c.post("/post-q?name=zoe")

    assert r.status_code == 200
    assert s.seen == [_Ping(name="zoe")]


async def test_the_body_wins_over_the_query_string(serve):
    s = await serve("PUT", "/post-bq")
    async with s.client() as c:
        r = await c.put("/post-bq?name=fromquery", json={"name": "frombody"})

    assert r.status_code == 200
    assert s.seen == [_Ping(name="frombody")]


async def test_a_body_that_is_not_json_counts_as_no_body(serve):
    s = await serve("POST", "/raw")
    async with s.client() as c:
        r = await c.post("/raw?name=zoe", content=b"not json")

    assert r.status_code == 200
    assert s.seen == [_Ping(name="zoe")]


async def test_a_bad_request_is_a_422_with_a_sentence(serve):
    s = await serve("POST", "/strict")
    async with s.client() as c:
        r = await c.post("/strict", json={"wrong": 1})

    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)
    assert s.seen == []


async def test_a_route_that_answers_with_its_lane_says_it_on_a_422_too(serve, monkeypatch):
    monkeypatch.setenv("LANE", "coe-here")
    s = await serve("POST", "/strict", answers_with_lane=True)
    async with s.client() as c:
        r = await c.post("/strict", json={"wrong": 1})

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["lane"] == "coe-here"
    assert isinstance(detail["message"], str) and detail["message"]


# ---------------------------------------------------------------------------
# the inner credential
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": SECRET}],
    ids=["none", "wrong", "not-bearer"],
)
async def test_a_route_that_needs_the_credential_refuses_without_it(serve, headers):
    s = await serve("POST", "/secret", inner_secret=True)
    async with s.client(**headers) as c:
        r = await c.post("/secret", json={"name": "x"})

    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert s.seen == []


async def test_the_right_credential_goes_through(serve):
    s = await serve("POST", "/secret", inner_secret=True)
    async with s.client(**_auth()) as c:
        r = await c.post("/secret", json={"name": "x"})

    assert r.status_code == 200
    assert len(s.seen) == 1


async def test_without_a_configured_credential_every_request_is_refused(serve, monkeypatch):
    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, inner_http_secret="")
    )
    s = await serve("POST", "/secret", inner_secret=True)
    async with s.client(**_auth()) as c:
        r = await c.post("/secret", json={"name": "x"})

    assert r.status_code == 503
    assert s.seen == []


async def test_the_credential_is_checked_before_the_parameters_are_read(serve):
    """Otherwise a caller without the credential could map the Data's fields from the 422s."""
    s = await serve("POST", "/secret", inner_secret=True)
    async with s.client() as c:
        r = await c.post("/secret", json={"wrong": 1})

    assert r.status_code == 401


async def test_the_credential_is_checked_before_the_lane(serve, monkeypatch):
    """A caller without the credential must not learn which lane it reached."""
    monkeypatch.setenv("LANE", "coe-here")
    s = await serve("POST", "/both", inner_secret=True, lane_match=True)
    async with s.client(**{"x-ctx-lane": "coe-there"}) as c:
        r = await c.post("/both", json={"name": "x"})

    assert r.status_code == 401


async def test_a_refusal_carries_the_lane_when_the_route_says_so(serve, monkeypatch):
    monkeypatch.setenv("LANE", "coe-here")
    s = await serve("POST", "/secret", inner_secret=True, answers_with_lane=True)
    async with s.client() as c:
        r = await c.post("/secret", json={"name": "x"})

    assert r.status_code == 401
    assert r.json()["detail"] == {"lane": "coe-here", "message": "missing or invalid credential"}


async def test_routes_without_the_credential_flag_are_open(serve):
    s = await serve("POST", "/open")
    async with s.client() as c:
        r = await c.post("/open", json={"name": "x"})

    assert r.status_code == 200


# ---------------------------------------------------------------------------
# the lane (ported from tests/runtime/test_http_source.py)
# ---------------------------------------------------------------------------


async def _lane_bound(serve, monkeypatch, *, process_lane: str | None, answers_with_lane=True):
    if process_lane is None:
        monkeypatch.delenv("LANE", raising=False)
    else:
        monkeypatch.setenv("LANE", process_lane)
    return await serve(
        "POST", "/lane-bound", lane_match=True, answers_with_lane=answers_with_lane
    )


async def test_a_request_for_another_lane_is_refused_before_the_handler(serve, monkeypatch):
    s = await _lane_bound(serve, monkeypatch, process_lane="coe-here")
    async with s.client(**{"x-ctx-lane": "coe-there"}) as c:
        r = await c.post("/lane-bound", json={"name": "x"})

    assert r.status_code == 409
    assert r.json()["detail"]["lane"] == "coe-here"
    assert "coe-there" in r.json()["detail"]["message"]
    assert s.seen == []


async def test_a_request_without_a_lane_is_meant_for_prod(serve, monkeypatch):
    s = await _lane_bound(serve, monkeypatch, process_lane="coe-here")
    async with s.client() as c:
        r = await c.post("/lane-bound", json={"name": "x"})

    assert r.status_code == 409
    assert s.seen == []


async def test_a_request_for_this_lane_goes_through(serve, monkeypatch):
    s = await _lane_bound(serve, monkeypatch, process_lane="coe-here")
    async with s.client(**{"x-ctx-lane": "coe-here"}) as c:
        r = await c.post("/lane-bound", json={"name": "x"})

    assert r.status_code == 200
    assert r.json() == {"name": "x"}
    assert len(s.seen) == 1


@pytest.mark.parametrize("header", [None, "prod"])
async def test_prod_takes_requests_without_a_lane_or_for_prod(serve, monkeypatch, header):
    s = await _lane_bound(serve, monkeypatch, process_lane=None)
    headers = {"x-ctx-lane": header} if header else {}
    async with s.client(**headers) as c:
        r = await c.post("/lane-bound", json={"name": "x"})

    assert r.status_code == 200
    assert len(s.seen) == 1


async def test_the_lane_is_checked_before_the_parameters_are_read(serve, monkeypatch):
    """A request that reached the wrong lane must not get a 422 that describes the parameters."""
    s = await _lane_bound(serve, monkeypatch, process_lane="coe-here")
    async with s.client(**{"x-ctx-lane": "coe-there"}) as c:
        r = await c.post("/lane-bound", json={"wrong": 1})

    assert r.status_code == 409
    assert s.seen == []


async def test_a_route_that_does_not_report_its_lane_refuses_with_a_sentence(serve, monkeypatch):
    s = await _lane_bound(serve, monkeypatch, process_lane="coe-here", answers_with_lane=False)
    async with s.client(**{"x-ctx-lane": "coe-there"}) as c:
        r = await c.post("/lane-bound", json={"name": "x"})

    assert r.status_code == 409
    assert isinstance(r.json()["detail"], str)


async def test_routes_without_the_declaration_are_not_checked(serve, monkeypatch):
    monkeypatch.setenv("LANE", "coe-here")
    s = await serve("POST", "/open")
    async with s.client(**{"x-ctx-lane": "coe-there"}) as c:
        r = await c.post("/open", json={"name": "x"})

    assert r.status_code == 200
    assert len(s.seen) == 1


# ---------------------------------------------------------------------------
# taking the route back
# ---------------------------------------------------------------------------


async def test_stop_takes_the_route_off_the_app_and_out_of_the_openapi_document():
    s = _Served("POST", "/gone")
    await s.host.start(http=s.app, schema=False, mq=False, clocks=False, tasks=False)
    assert "/gone" in s.app.openapi()["paths"]

    await s.host.stop()

    assert s.app.openapi_schema is None
    assert "/gone" not in s.app.openapi()["paths"]
    async with s.client() as c:
        assert (await c.post("/gone", json={"name": "x"})).status_code == 404


async def test_a_restarted_host_puts_the_route_back_into_the_openapi_document():
    """The app caches its OpenAPI document the first time it is asked for; binding a route drops
    that cache, as taking one back does, or the document would keep saying the route is gone."""
    s = _Served("POST", "/again")
    await s.host.start(http=s.app, schema=False, mq=False, clocks=False, tasks=False)
    await s.host.stop()
    assert "/again" not in s.app.openapi()["paths"]

    await s.host.start(http=s.app, schema=False, mq=False, clocks=False, tasks=False)
    try:
        assert "/again" in s.app.openapi()["paths"]
        async with s.client() as c:
            assert (await c.post("/again", json={"name": "x"})).status_code == 200
    finally:
        await s.host.stop()


async def test_an_unsupported_method_is_refused_at_registration():
    async def handler(p: _Ping) -> None:  # pragma: no cover
        return None

    host = Host("agent-service", [plugin("p", lambda ctx: ctx.route("PATCH", "/x", _Ping, handler))])

    with pytest.raises(ValueError, match="PATCH"):
        await host.start(http=None, schema=False, mq=False, clocks=False, tasks=False)
