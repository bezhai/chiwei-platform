"""人工参与者的入口：运维以任意身份发消息、提问、定时送达，查记录。

这一组验的是 HTTP 这一层：凭据、泳道自报与泳道不符时拒绝、参数、错误翻成状态码。
通信机制本身的行为在 ``test_contract.py`` 里用真 broker 验过，这里把它换成替身，只看
入口有没有把请求原样交给它、把结果原样交回来。
"""
from __future__ import annotations

import dataclasses
import importlib
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from app.infra import config
from app.messaging.message import Answer, Delivery, SendFailed
from app.runtime.http_source import register_http_sources

SECRET = "operator-entry-secret"
LANE = "coe-msg"
BASE = "http://operator.test"


@pytest.fixture
def calls(monkeypatch):
    """把通信机制换成替身，记下入口交给它的每一次调用。"""
    from app.messaging import operator

    seen: list[tuple[str, dict]] = []

    async def send(**kw):
        seen.append(("send", kw))
        if kw["recipient"] == "nobody":
            return Delivery("m-1", delivered=False, reason="对方没有开设收件箱")
        if kw["recipient"] == "broken":
            raise SendFailed("broker did not confirm", message_id="m-broken")
        return Delivery("m-1", delivered=True)

    async def ask(**kw):
        seen.append(("ask", kw))
        return Answer("q-1", "厨房里灯亮着。")

    async def send_at(**kw):
        # 跟真的 send_at 一样：不带时区的时刻不收（真实实现的这一条在
        # test_message_shape.py 里验）。这里验的是入口把它翻成 400。
        if kw["at"].tzinfo is None:
            raise ValueError("send_at needs a timezone-aware time")
        seen.append(("send_at", kw))
        return "s-1"

    async def read_record(**kw):
        seen.append(("read_record", kw))
        return [{"message_id": "m-1", "outcome": "delivered"}]

    async def peek_dead_letters(**kw):
        seen.append(("peek_dead_letters", kw))
        return [{"message": {"message_id": "d-1"}, "origin": "inbox_world_coe-msg"}]

    async def replay_dead_letters(**kw):
        seen.append(("replay_dead_letters", kw))
        return {"replayed": 1, "refused": 0, "failed": 0}

    monkeypatch.setattr(operator, "peek_dead_letters", peek_dead_letters)
    monkeypatch.setattr(operator, "replay_dead_letters", replay_dead_letters)
    monkeypatch.setattr(operator, "send", send)
    monkeypatch.setattr(operator, "ask", ask)
    monkeypatch.setattr(operator, "send_at", send_at)
    monkeypatch.setattr(operator, "read_record", read_record)
    return seen


def _reload_messaging_wiring():
    import app.wiring.messaging as messaging_wiring
    from app.messaging.receiving import clear_inboxes
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_inboxes()
    importlib.reload(messaging_wiring)


@pytest.fixture
def api(monkeypatch, calls) -> FastAPI:
    monkeypatch.setenv("LANE", LANE)
    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, inner_http_secret=SECRET)
    )
    _reload_messaging_wiring()
    application = FastAPI()
    from app.api.middleware import HeaderContextMiddleware

    application.add_middleware(HeaderContextMiddleware)
    register_http_sources(application)
    return application


@pytest.fixture
async def client(api):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url=BASE,
        headers={"Authorization": f"Bearer {SECRET}", "x-ctx-lane": LANE},
    ) as c:
        yield c


async def test_send_as_anyone(client, calls):
    r = await client.post(
        "/admin/messaging/send",
        json={"sender": "akao", "recipient": "world", "body": "我走进了厨房。"},
    )

    assert r.status_code == 200
    assert r.json() == {
        "lane": LANE,
        "message_id": "m-1",
        "delivered": True,
        "reason": None,
    }
    assert calls == [
        (
            "send",
            {
                "sender": "akao",
                "recipient": "world",
                "body": "我走进了厨房。",
                "message_id": None,
                "wakes_recipient": True,
            },
        )
    ]


async def test_send_can_say_not_to_wake_the_recipient(client, calls):
    """不说就叫醒；可以显式说不叫醒，方便验证收件方的处理。"""
    r = await client.post(
        "/admin/messaging/send",
        json={
            "sender": "world",
            "recipient": "赤尾",
            "body": "窗外起风了。",
            "wakes_recipient": False,
        },
    )

    assert r.status_code == 200
    assert calls[-1][1]["wakes_recipient"] is False


async def test_a_failed_send_can_be_retried_with_the_same_id(client, calls):
    """发送失败时回答里带着消息 id；带着它重试，接收方按 id 去重。"""
    failed = await client.post(
        "/admin/messaging/send",
        json={"sender": "operator", "recipient": "broken", "body": "x"},
    )
    assert failed.status_code == 503
    message_id = failed.json()["detail"]["message_id"]
    assert message_id == "m-broken"

    await client.post(
        "/admin/messaging/send",
        json={"sender": "operator", "recipient": "world", "body": "x", "message_id": message_id},
    )
    assert calls[-1][1]["message_id"] == "m-broken"


async def test_send_to_an_inbox_nobody_opened_says_so(client, calls):
    r = await client.post(
        "/admin/messaging/send",
        json={"sender": "operator", "recipient": "nobody", "body": "在吗"},
    )

    assert r.status_code == 200
    assert r.json()["delivered"] is False
    assert r.json()["reason"] == "对方没有开设收件箱"


async def test_ask(client, calls):
    r = await client.post(
        "/admin/messaging/ask",
        json={
            "sender": "operator",
            "recipient": "world",
            "body": "厨房现在什么样？",
            "timeout_seconds": 30,
        },
    )

    assert r.status_code == 200
    assert r.json() == {
        "lane": LANE,
        "question_id": "q-1",
        "answered": True,
        "answer": "厨房里灯亮着。",
        "reason": None,
    }
    assert calls[0][1]["timeout_seconds"] == 30


async def test_ask_timeout_is_bounded(client, calls):
    r = await client.post(
        "/admin/messaging/ask",
        json={"sender": "operator", "recipient": "world", "body": "?", "timeout_seconds": 3600},
    )
    assert r.status_code == 422
    assert calls == []


async def test_send_at(client, calls):
    at = "2026-09-29T18:30:00+08:00"
    r = await client.post(
        "/admin/messaging/send-at",
        json={"sender": "operator", "recipient": "operator", "body": "提醒我。", "at": at},
    )

    assert r.status_code == 200
    assert r.json() == {"lane": LANE, "message_id": "s-1", "deliver_at": at}
    assert calls[0][1]["at"] == datetime.fromisoformat(at)
    assert calls[0][1]["wakes_recipient"] is True


async def test_send_at_can_say_not_to_wake_the_recipient(client, calls):
    r = await client.post(
        "/admin/messaging/send-at",
        json={
            "sender": "world",
            "recipient": "赤尾",
            "body": "快递到了。",
            "at": "2026-09-29T18:30:00+08:00",
            "wakes_recipient": False,
        },
    )

    assert r.status_code == 200
    assert calls[0][1]["wakes_recipient"] is False


async def test_send_at_needs_a_timezone(client, calls):
    r = await client.post(
        "/admin/messaging/send-at",
        json={"sender": "operator", "recipient": "operator", "body": "x", "at": "2026-09-29T18:30:00"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["lane"] == LANE
    assert calls == []


async def test_read_the_record(client, calls):
    r = await client.get(
        "/admin/messaging/record", params={"participant": "world", "limit": "20"}
    )

    assert r.status_code == 200
    assert r.json() == {"lane": LANE, "rows": [{"message_id": "m-1", "outcome": "delivered"}]}
    assert calls == [("read_record", {"message_id": None, "participant": "world", "limit": 20})]


async def test_look_at_this_lanes_dead_letters(client, calls):
    r = await client.get("/admin/messaging/dead-letters", params={"limit": "5"})

    assert r.status_code == 200
    assert r.json() == {
        "lane": LANE,
        "dead_letters": [{"message": {"message_id": "d-1"}, "origin": "inbox_world_coe-msg"}],
    }
    assert calls == [("peek_dead_letters", {"limit": 5})]


async def test_replay_this_lanes_dead_letters(client, calls):
    r = await client.post(
        "/admin/messaging/dead-letters/replay",
        json={"limit": 3},
        headers={"X-Operator": "bezhai"},
    )

    assert r.status_code == 200
    assert r.json() == {"lane": LANE, "replayed": 1, "refused": 0, "failed": 0}
    assert calls == [("replay_dead_letters", {"limit": 3, "operator": "bezhai"})]


async def test_a_replay_meant_for_another_lane_is_refused(api, calls):
    """ppe 和 prod 共用 broker：落错泳道的重放请求一条都不动。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url=BASE,
        headers={"Authorization": f"Bearer {SECRET}", "x-ctx-lane": "prod"},
    ) as c:
        r = await c.post("/admin/messaging/dead-letters/replay", json={"limit": 3})
    assert r.status_code == 409
    assert calls == []


async def test_chinese_participant_names_pass_through_body_and_query(client, calls):
    """参与者的名字就是它在世界里的名字：请求体里的、查询参数里的中文名都原样交给通信机制。"""
    sent = await client.post(
        "/admin/messaging/send",
        json={"sender": "千凪", "recipient": "赤尾", "body": "姐姐，晚饭好了。"},
    )
    rows = await client.get("/admin/messaging/record", params={"participant": "赤尾"})

    assert sent.status_code == 200 and rows.status_code == 200
    assert calls == [
        (
            "send",
            {
                "sender": "千凪",
                "recipient": "赤尾",
                "body": "姐姐，晚饭好了。",
                "message_id": None,
                "wakes_recipient": True,
            },
        ),
        ("read_record", {"message_id": None, "participant": "赤尾", "limit": 50}),
    ]


async def test_a_bad_participant_name_is_a_bad_request(client, calls, monkeypatch):
    from app.messaging import operator

    async def refuse(**kw):
        raise ValueError("participant name 'A.B' must match ...")

    monkeypatch.setattr(operator, "send", refuse)
    r = await client.post(
        "/admin/messaging/send", json={"sender": "A.B", "recipient": "world", "body": "x"}
    )
    assert r.status_code == 400
    assert r.json()["detail"]["lane"] == LANE


async def test_a_send_that_did_not_go_out_is_a_503(client, calls):
    r = await client.post(
        "/admin/messaging/send",
        json={"sender": "operator", "recipient": "broken", "body": "x"},
    )
    assert r.status_code == 503
    assert r.json()["detail"]["lane"] == LANE


@pytest.mark.parametrize(
    "path,method,body",
    [
        ("/admin/messaging/send", "POST", {"sender": "a", "recipient": "b", "body": "c"}),
        ("/admin/messaging/ask", "POST", {"sender": "a", "recipient": "b", "body": "c"}),
        (
            "/admin/messaging/send-at",
            "POST",
            {"sender": "a", "recipient": "b", "body": "c", "at": datetime.now(UTC).isoformat()},
        ),
        ("/admin/messaging/record", "GET", None),
        ("/admin/messaging/dead-letters", "GET", None),
        ("/admin/messaging/dead-letters/replay", "POST", {"limit": 1}),
    ],
)
async def test_every_route_needs_the_inner_credential(api, calls, path, method, body):
    """能冒充任何人往收件箱里塞东西的口子，不能是裸的。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url=BASE, headers={"x-ctx-lane": LANE}
    ) as bare:
        r = await bare.request(method, path, json=body)
    assert r.status_code == 401
    assert calls == []


async def test_a_request_meant_for_another_lane_is_refused_and_nothing_is_sent(api, calls):
    """泳道没部署时 sidecar 会把请求落回 prod：落错了的请求一条都不发，并说出自己在哪。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url=BASE,
        headers={"Authorization": f"Bearer {SECRET}", "x-ctx-lane": "coe-elsewhere"},
    ) as c:
        r = await c.post(
            "/admin/messaging/send", json={"sender": "a", "recipient": "b", "body": "c"}
        )
    assert r.status_code == 409
    assert r.json()["detail"]["lane"] == LANE
    assert calls == []


async def test_a_request_without_a_lane_lands_only_on_prod(api, calls):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url=BASE,
        headers={"Authorization": f"Bearer {SECRET}"},
    ) as c:
        r = await c.post(
            "/admin/messaging/send", json={"sender": "a", "recipient": "b", "body": "c"}
        )
    assert r.status_code == 409
    assert calls == []


def test_the_operator_owns_an_inbox_in_agent_service():
    """人工参与者自己有收件箱：发给它、定时发给它都能送到，内容在记录里看。"""
    from app.messaging.receiving import INBOX_REGISTRY

    _reload_messaging_wiring()
    assert "operator" in INBOX_REGISTRY
    assert INBOX_REGISTRY["operator"].on_question is None
