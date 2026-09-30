"""world 记录的人工读写接口：列目录、读一份、写一份、删一份。

跑在真的 HTTP 这一层上（凭据、泳道核对、参数、错误码），记录落在临时的私有卷上。
凭据和泳道核对跟通信机制的人工入口是同一套声明（``requires_inner_secret`` /
``requires_lane_match`` / ``answers_with_lane``）。
"""
from __future__ import annotations

import dataclasses
import importlib
import logging

import httpx
import pytest
from fastapi import FastAPI

from app.infra import config
from app.runtime.http_source import register_http_sources
from app.world import records

from .conftest import LANE

SECRET = "world-admin-secret"
BASE = "http://world.test"
LIST = "/admin/world/records"
DOC = "/admin/world/records/document"


@pytest.fixture
def api(volume, monkeypatch) -> FastAPI:
    monkeypatch.setenv("APP_NAME", "world")
    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, inner_http_secret=SECRET)
    )
    import app.world.wiring as wiring
    from app.messaging.receiving import clear_inboxes
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    clear_inboxes()
    importlib.reload(wiring)
    application = FastAPI()
    from app.api.middleware import HeaderContextMiddleware

    application.add_middleware(HeaderContextMiddleware)
    register_http_sources(application)
    return application


def _client(api, **headers) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url=BASE, headers=headers
    )


@pytest.fixture
async def client(api):
    async with _client(api, Authorization=f"Bearer {SECRET}", **{"x-ctx-lane": LANE}) as c:
        yield c


async def test_list_read_write_and_delete(client):
    created = await client.put(DOC, json={"path": "地方/甲.md", "text": "朝北的窗。"})
    assert created.status_code == 200
    body = created.json()
    assert body["lane"] == LANE and body["created"] is True
    assert body["previous_fingerprint"] is None
    fp = body["fingerprint"]

    listed = await client.get(LIST)
    assert listed.status_code == 200
    assert listed.json()["lane"] == LANE
    [entry] = listed.json()["records"]
    assert (entry["path"], entry["chars"], entry["fingerprint"]) == ("地方/甲.md", 5, fp)

    read = await client.get(DOC, params={"path": "地方/甲.md"})
    assert read.status_code == 200
    assert read.json()["text"] == "朝北的窗。" and read.json()["fingerprint"] == fp

    rewritten = await client.put(
        DOC, json={"path": "地方/甲.md", "text": "朝北的窗，窗台上有盆花。", "fingerprint": fp}
    )
    assert rewritten.status_code == 200
    assert rewritten.json()["created"] is False
    assert rewritten.json()["previous_fingerprint"] == fp
    new_fp = rewritten.json()["fingerprint"]

    deleted = await client.delete(DOC, params={"path": "地方/甲.md", "fingerprint": new_fp})
    assert deleted.status_code == 200
    assert deleted.json() == {"lane": LANE, "path": "地方/甲.md", "fingerprint": new_fp}
    assert records.listing() == []


async def test_rewriting_without_or_with_a_stale_fingerprint_changes_nothing(client):
    first = records.write("人/乙.md", "第一版。", expected=None)
    records.write("人/乙.md", "world 刚改的。", expected=first.fingerprint)

    blind = await client.put(DOC, json={"path": "人/乙.md", "text": "盲写。"})
    stale = await client.put(
        DOC, json={"path": "人/乙.md", "text": "旧指纹。", "fingerprint": first.fingerprint}
    )
    stale_delete = await client.delete(
        DOC, params={"path": "人/乙.md", "fingerprint": first.fingerprint}
    )

    assert [r.status_code for r in (blind, stale, stale_delete)] == [409, 409, 409]
    assert all(r.json()["detail"]["lane"] == LANE for r in (blind, stale, stale_delete))
    assert records.read("人/乙.md").text == "world 刚改的。"


async def test_a_missing_record_is_404(client):
    read = await client.get(DOC, params={"path": "没有/这份.md"})
    delete = await client.delete(DOC, params={"path": "没有/这份.md", "fingerprint": "x"})

    assert (read.status_code, delete.status_code) == (404, 404)
    assert read.json()["detail"]["lane"] == LANE


@pytest.mark.parametrize("path", ["../next_wake.json", "../records.md", "/甲.md", "甲.txt"])
async def test_nothing_outside_the_records_tree_is_reachable(client, volume, path):
    (volume / LANE).mkdir(parents=True, exist_ok=True)
    (volume / LANE / "next_wake.json").write_text("{}", encoding="utf-8")

    read = await client.get(DOC, params={"path": path})
    write = await client.put(DOC, json={"path": path, "text": "x"})

    assert (read.status_code, write.status_code) == (400, 400)
    assert (volume / LANE / "next_wake.json").read_text(encoding="utf-8") == "{}"


async def test_oversized_text_is_a_bad_request(client):
    r = await client.put(
        DOC, json={"path": "甲.md", "text": "字" * (records.MAX_RECORD_CHARS + 1)}
    )

    assert r.status_code == 400
    assert records.listing() == []


async def test_writes_and_deletes_are_logged_with_the_operator(client, caplog):
    caplog.set_level(logging.INFO, logger="app.world.admin")

    r = await client.put(
        DOC, json={"path": "甲.md", "text": "一。"}, headers={"X-Operator": "bezhai"}
    )
    await client.delete(
        DOC,
        params={"path": "甲.md", "fingerprint": r.json()["fingerprint"]},
        headers={"X-Operator": "bezhai"},
    )

    lines = [rec.getMessage() for rec in caplog.records if rec.name == "app.world.admin"]
    assert len(lines) == 2
    assert all("bezhai" in line and "甲.md" in line for line in lines)


async def test_without_a_volume_the_answer_says_so(client, monkeypatch):
    monkeypatch.delenv("WORLD_DATA_DIR")

    r = await client.get(LIST)

    assert r.status_code == 503
    assert r.json()["detail"]["lane"] == LANE


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("GET", LIST, {}),
        ("GET", DOC, {"params": {"path": "甲.md"}}),
        ("PUT", DOC, {"json": {"path": "甲.md", "text": "x"}}),
        ("DELETE", DOC, {"params": {"path": "甲.md", "fingerprint": "x"}}),
    ],
)
async def test_every_route_needs_the_inner_credential(api, method, path, kwargs):
    async with _client(api, **{"x-ctx-lane": LANE}) as bare:
        r = await bare.request(method, path, **kwargs)

    assert r.status_code == 401
    assert records.listing() == []


async def test_a_request_meant_for_another_lane_changes_nothing(api):
    async with _client(
        api, Authorization=f"Bearer {SECRET}", **{"x-ctx-lane": "coe-elsewhere"}
    ) as c:
        r = await c.put(DOC, json={"path": "甲.md", "text": "落错泳道。"})

    assert r.status_code == 409
    assert r.json()["detail"]["lane"] == LANE
    assert records.listing() == []


async def test_a_request_without_a_lane_is_meant_for_prod_and_refused_here(api):
    async with _client(api, Authorization=f"Bearer {SECRET}") as c:
        r = await c.get(LIST)

    assert r.status_code == 409


def test_the_routes_run_in_the_world_app():
    """HTTP 路由挂在跑它消费者的那个 App 的进程里：这四条的节点都绑在 world 上。"""
    import app.world.wiring as wiring
    from app.runtime.placement import clear_bindings, nodes_for_app
    from app.runtime.wire import WIRING_REGISTRY, clear_wiring

    clear_wiring()
    clear_bindings()
    importlib.reload(wiring)

    http_consumers = {
        c for w in WIRING_REGISTRY if any(s.kind == "http" for s in w.sources) for c in w.consumers
    }
    assert len(http_consumers) == 4
    assert http_consumers <= nodes_for_app("world")
    assert not http_consumers & nodes_for_app("agent-service")
