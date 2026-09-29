"""消息长什么样、参与者叫什么、定时送达怎么分段 —— 不碰 broker 的那几条。"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.infra.rabbitmq import X_DELAY_MAX_MS
from app.messaging.message import Kind, Message, new_message, participant


@pytest.mark.parametrize("name", ["world", "akao", "operator", "ayana-2", "npc_teacher"])
def test_participant_names_that_are_accepted(name):
    assert participant(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "World", "a.b", "a*", "#", "赤尾", "-lead", "x" * 64, None, 3],
)
def test_participant_names_that_are_refused(name):
    """名字直接进队列名和 routing key：点、通配符、大写、非 ASCII 一律不收。"""
    with pytest.raises(ValueError):
        participant(name)


def test_the_envelope_carries_exactly_id_sender_recipient_time_and_kind():
    """外层只有这五样加正文，没有地点编号、没有房间 id。"""
    assert [f.name for f in dataclasses.fields(Message)] == [
        "message_id",
        "sender",
        "recipient",
        "time",
        "kind",
        "body",
    ]


def test_a_message_survives_the_wire_unchanged():
    m = new_message(
        sender="operator",
        recipient="world",
        body="赤尾在 18:02 走进了厨房，打开冰箱。",
        kind=Kind.MESSAGE,
    )
    back = Message.from_json(m.to_json())
    assert back == m
    assert back.time.tzinfo is not None


def test_new_messages_get_distinct_ids_and_an_aware_time():
    a = new_message(sender="world", recipient="world", body="x", kind=Kind.MESSAGE)
    b = new_message(sender="world", recipient="world", body="x", kind=Kind.MESSAGE)
    assert a.message_id != b.message_id
    assert a.time.tzinfo is not None


def test_a_retry_keeps_the_original_message_id():
    m = new_message(
        sender="world", recipient="akao", body="x", kind=Kind.MESSAGE, message_id="abc-123_X"
    )
    assert m.message_id == "abc-123_X"


@pytest.mark.parametrize("bad", ["", "has space", "a.b", "x" * 65, 7])
def test_a_malformed_message_id_is_refused(bad):
    with pytest.raises(ValueError):
        new_message(sender="world", recipient="akao", body="x", kind=Kind.MESSAGE, message_id=bad)


@pytest.mark.parametrize("body", ["", "   ", None])
def test_an_empty_body_is_refused(body):
    with pytest.raises(ValueError):
        new_message(sender="world", recipient="akao", body=body, kind=Kind.MESSAGE)


def test_hop_delay_is_capped_by_the_broker_limit():
    from app.messaging.broker import hop_delay_ms

    now = datetime(2026, 9, 29, tzinfo=UTC)
    assert hop_delay_ms(now + timedelta(seconds=3), now) == 3000
    assert hop_delay_ms(now - timedelta(seconds=3), now) == 0
    assert hop_delay_ms(now + timedelta(days=90), now) == X_DELAY_MAX_MS


async def test_send_at_has_no_business_level_upper_bound(monkeypatch):
    """排三个月以后也收：超过 broker 延时上限的部分由机制自己分段，对使用者透明。"""
    from app.messaging import sending

    published: list[dict] = []

    async def fake_publish(route, body, *, headers, delay_ms=None):
        published.append({"route": route, "delay_ms": delay_ms, "body": body})

    async def fake_record(*a, **kw):
        return None

    monkeypatch.setattr(sending, "publish", fake_publish)
    monkeypatch.setattr(sending, "record", fake_record)

    at = datetime.now(UTC) + timedelta(days=90)
    message_id = await sending.send_at(
        sender="world", recipient="world", body="三个月后的这一天", at=at
    )

    assert published and published[0]["delay_ms"] == X_DELAY_MAX_MS
    assert published[0]["body"]["message_id"] == message_id
    assert Message.from_json(published[0]["body"]).time == at


async def test_send_at_refuses_a_time_without_a_timezone():
    from app.messaging.sending import send_at

    with pytest.raises(ValueError):
        await send_at(
            sender="world", recipient="world", body="x", at=datetime(2026, 10, 1, 8, 0)
        )


async def test_send_at_refuses_to_schedule_without_the_delayed_exchange(monkeypatch):
    """没有 x-delayed-message 插件时，延时头被忽略、定时消息会在分段那一步空转。宁可当场拒绝。"""
    from app.messaging import sending

    monkeypatch.setenv("RABBITMQ_DISABLE_DELAYED", "1")
    monkeypatch.setattr(sending, "publish", AsyncMock())
    with pytest.raises(sending.SendFailed):
        await sending.send_at(
            sender="world",
            recipient="world",
            body="x",
            at=datetime.now(UTC) + timedelta(minutes=5),
        )
    sending.publish.assert_not_called()
