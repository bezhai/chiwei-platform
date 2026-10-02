"""消息长什么样、参与者叫什么、定时送达怎么分段 —— 不碰 broker 的那几条。"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.infra.rabbitmq import X_DELAY_MAX_MS
from app.messaging.message import Kind, Message, broker_form, new_message, participant


def _chinese_name(length: int) -> str:
    """``length`` 个互不相同的汉字。"""
    return "".join(chr(0x4E00 + 211 * i) for i in range(length))


# 名字在 broker 上的写法最长 128 个字符。ASCII 名字就是它本身；中文名是 ":" 加 punycode，
# 这 43 个字写出来是 127 个字符，再多一个就是 130 个。
_LONGEST_ASCII = "x" * 128
_LONG_CHINESE = _chinese_name(43)


@pytest.mark.parametrize(
    "name",
    [
        "world",
        "operator",
        "ayana-2",
        "npc_teacher",
        "赤尾",
        "千凪",
        "绫奈",
        "佐々木",
        "アリス",
        "World",
        "3号",
        _LONGEST_ASCII,
        _LONG_CHINESE,
    ],
)
def test_participant_names_that_are_accepted(name):
    """参与者的名字就是它在世界里的名字：中文、日文、大小写、数字都收，原样交回。"""
    assert participant(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "a.b",  # routing key 的分隔符
        "赤尾.",
        "a*",  # 写进绑定就成了通配符
        "#",
        "赤 尾",  # 空白
        "赤\t尾",
        "　赤尾",  # 全角空格
        "赤\x00尾",  # 控制字符；NUL 也进不了 Postgres 的 text
        "a/b",
        "a:b",  # 冒号留给非 ASCII 名字在 broker 上的写法
        "赤尾！",
        "-lead",
        "_lead",
        _LONGEST_ASCII + "x",
        _chinese_name(44),
        None,
        3,
    ],
)
def test_participant_names_that_are_refused(name):
    """名字要拼进队列名和 routing key：点、通配符、空白、控制字符、标点、过长的一律不收。"""
    with pytest.raises(ValueError):
        participant(name)


def test_how_a_name_is_written_on_the_broker():
    """AMQP 客户端只收 ASCII 的队列名：ASCII 名字原样用，别的写成 ":" 加 punycode，能原样解回来。

    这是队列名的一部分，改了写法，已经开设的收件箱就对不上了。
    """
    assert broker_form("world") == "world"
    assert broker_form("World") == "World"
    assert broker_form("赤尾") == ":bgtr75i"
    assert broker_form("赤尾_2") == ":_2-ds1dl11p"
    for name in ("赤尾", "千凪", "アリス", "赤尾_2", _LONG_CHINESE):
        form = broker_form(name)
        assert form.isascii()
        assert form[1:].encode().decode("punycode") == name


def test_the_longest_name_in_the_longest_lane_fits_every_queue_name_and_routing_key():
    """队列名和 routing key 都是 AMQP 的 shortstr，最长 255 个字节。

    泳道是 K8s 资源名 ``<App>-<泳道>`` 的一部分，那个名字最长 63 个字符，泳道只会更短，
    这里按 63 个字节算。
    """
    from app.infra.rabbitmq import _lane_rk, lane_queue
    from app.messaging.broker import inbox_route

    longest_lane = "coe-" + "x" * 59
    assert len(longest_lane) == 63
    for name in (_LONGEST_ASCII, _LONG_CHINESE):
        for route in (inbox_route(name),):
            assert len(lane_queue(route.queue, longest_lane).encode()) <= 255
            assert len(_lane_rk(route.rk, longest_lane).encode()) <= 255


def test_a_message_has_exactly_id_sender_recipient_time_kind_and_body():
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
