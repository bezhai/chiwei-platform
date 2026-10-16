"""旧读书队列的搬运（``POST /admin/reading/carry-over``），跑在真 broker + 真 Postgres 上。

旧队列按旧版本声明它们的方式建出来（``mq.declare_route``，泳道取进程的泳道），队列参数因此和
线上一样：泳道队列 10 秒 TTL、按 prod 的 routing key 死信回不带后缀的那一条；prod 队列死信进共用的
``dead_letters``。里面的几种消息也由 broker 自己造出来：泳道队列里过期的、被拒收的，都是 broker
按这些参数送进不带后缀的那一条的，``x-death`` 头是 broker 写的。

读书收件箱由 living 插件开设，通信机制照线上那样开始接收；读一程本身换成替身，只记下读了哪几程。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from types import SimpleNamespace

import aio_pika
import pytest

from app.infra.rabbitmq import DLQ_NAME, Route, lane_queue, mq
from app.living import participants as participants_mod
from app.living import reading as reading_mod
from app.living.participants import READING
from app.living.reading import FileRead
from app.messaging.lifecycle import start_messaging
from app.messaging.record import read_record
from app.nodes.reading_carry_over import ReadingCarryOverRequest, carry_over_reading
from app.plugins import living as living_plugin
from app.plugins import skills as skills_plugin
from tests.living.conftest import RESIDENT_NAMES

# 真 broker（带延时插件）+ 通信机制那几张表，跟 ``tests/messaging`` 用同一份。
from tests.messaging.conftest import (  # noqa: F401 — 形参名就是 fixture 名
    LANE,
    broker,
    delayed_broker,
    messaging_db,
)
from tests.messaging.helpers import eventually
from tests.runtime.conftest import migrate

pytestmark = pytest.mark.integration

# 旧持久边的那条路由：队列 ``durable_<数据>_<消费者>``，routing key ``durable.<数据>.<消费者>``。
OLD = Route("durable_file_picked_up_read_a_round", "durable.file_picked_up.read_a_round")
OWN = lane_queue(OLD.queue, LANE)
FALLBACK = OLD.queue
OTHER_LANE = "coe-other"


class _Round:
    """读一程的替身：记下读了哪几程，每一程都"没读成"（不写 ``FileRead``）。"""

    def __init__(self) -> None:
        self.round_ids: list[str] = []

    async def __call__(self, **kwargs):
        self.round_ids.append(kwargs["round_id"])
        return None


def _body(round_id: str, *, lane: str = LANE, persona_id: str = "akao") -> dict:
    """旧版本发进持久边的消息体：那条 ``FilePickedUp`` Data 的 JSON。"""
    return {
        "lane": lane,
        "round_id": round_id,
        "persona_id": persona_id,
        "attachment_id": f"msg-{round_id}:key-a",
        "title": f"{round_id}.txt",
        "tos_file": "files/key-a",
    }


async def _publish_old(body: dict | bytes, *, lane: str | None, expiration=None) -> None:
    """照旧版本的 ``publish_durable`` 发到旧路由上：主交换机、带泳道后缀的 routing key。"""
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    rk = f"{OLD.rk}.{lane}" if lane else OLD.rk
    headers = {"data_type": "FilePickedUp", "trace_id": "", "lane": lane or ""}
    await mq._exchange.publish(  # type: ignore[union-attr]
        aio_pika.Message(raw, headers=headers, expiration=expiration), routing_key=rk
    )


async def _reject_once(lane: str | None, body: dict) -> None:
    """发一条到这条泳道的旧队列，取出来拒收：broker 按队列参数把它送去死信。"""
    await _publish_old(body, lane=lane)
    channel = await mq._connection.channel()  # type: ignore[union-attr]
    try:
        queue = await channel.declare_queue(lane_queue(OLD.queue, lane), passive=True)
        incoming = await queue.get(no_ack=False, timeout=5)
        await incoming.reject(requeue=False)
    finally:
        await channel.close()


async def _bodies(queue_name: str) -> list[bytes]:
    """队列里现在的每一条消息体（看完放回）。"""
    channel = await mq._connection.channel()  # type: ignore[union-attr]
    taken = []
    try:
        queue = await channel.declare_queue(queue_name, passive=True)
        while (incoming := await queue.get(no_ack=False, fail=False)) is not None:
            taken.append(incoming)
        return [m.body for m in taken]
    finally:
        for m in taken:
            await m.nack(requeue=True)
        await channel.close()


async def _the_shared_dead_letter_was_never_taken(broker) -> None:  # noqa: F811
    """共用的 ``dead_letters`` 里那一条还在，而且没被谁取出来过（取出来再放回的消息 broker 会标成重投）。

    这一查本身也把它取出来再放回，所以一个用例只在最后查一次。
    """
    assert await broker.depth(DLQ_NAME) == 1
    channel = await mq._connection.channel()  # type: ignore[union-attr]
    try:
        queue = await channel.declare_queue(DLQ_NAME, passive=True)
        incoming = await queue.get(no_ack=False, timeout=5)
        assert incoming.redelivered is False
        assert json.loads(incoming.body)["round_id"] == "prod-dead"
        await incoming.nack(requeue=True)
    finally:
        await channel.close()


@pytest.fixture
async def reading_inbox(broker, messaging_db, living_db, app_host, monkeypatch):  # noqa: F811
    """living 插件开好的读书收件箱在本进程的泳道上收消息。交回读一程的替身。"""

    async def find_persona(persona_id: str):
        return SimpleNamespace(persona_id=persona_id, display_name=RESIDENT_NAMES[persona_id])

    await migrate(FileRead, living_db)
    monkeypatch.setattr(participants_mod, "find_persona", find_persona)
    one_round = _Round()
    monkeypatch.setattr(reading_mod, "run_reading_round", one_round)

    async def start() -> _Round:
        await app_host("agent-service", [skills_plugin.PLUGIN, living_plugin.PLUGIN])
        await start_messaging()
        return one_round

    return start


async def _carry(**fields) -> dict:
    answer = await carry_over_reading(ReadingCarryOverRequest(**fields))
    return answer.model_dump()


def _by_queue(answer: dict) -> dict[str, dict]:
    return {q["queue"]: q for q in answer["queues"]}


async def _seed_a_lane(broker) -> None:  # noqa: F811
    """泳道 ``LANE`` 上：本泳道的旧队列、不带后缀的那一条，各种消息各一条；共用死信里一条 prod 的。"""
    await mq.declare_route(OLD, lane=None)
    await mq.declare_route(OLD, lane=LANE)
    # prod 被拒收的那一条进共用的 dead_letters；搬运不该碰它。
    await _reject_once(None, _body("prod-dead", lane="prod"))
    # 不带后缀的那一条：本泳道 10 秒没人消费的（这里让它 1 毫秒就过期）、本泳道被拒收的、
    # 别的泳道被拒收的、prod 的、解不开的。
    await _publish_old(_body("expired"), lane=LANE, expiration=timedelta(milliseconds=1))
    await _reject_once(LANE, _body("dead"))
    await mq.declare_route(OLD, lane=OTHER_LANE)
    await _reject_once(OTHER_LANE, _body("other-lane", lane=OTHER_LANE))
    await _publish_old(_body("prod-pending", lane="prod"), lane=None)
    await _publish_old(b"not json at all", lane=None)
    await eventually(lambda: _depth_is(broker, FALLBACK, 5))
    # 本泳道的旧队列：一条没人处理过的。
    await _publish_old(_body("pending"), lane=LANE)
    await eventually(lambda: _depth_is(broker, OWN, 1))


async def _depth_is(broker, name: str, depth: int) -> bool:  # noqa: F811
    return await broker.depth(name) == depth


async def test_a_dry_run_reports_every_kind_in_both_queues_and_changes_nothing(
    broker, reading_inbox  # noqa: F811
):
    one_round = await reading_inbox()
    await _seed_a_lane(broker)

    answer = await _carry()

    assert answer["lane"] == LANE
    assert answer["dry_run"] is True
    queues = _by_queue(answer)
    assert list(queues) == [OWN, FALLBACK]
    own, fallback = queues[OWN], queues[FALLBACK]
    assert own["exists"] is True and fallback["exists"] is True
    assert (own["depth_before"], own["unprocessed"], own["dead"], own["foreign"]) == (1, 1, 0, 0)
    assert (
        fallback["depth_before"],
        fallback["unprocessed"],
        fallback["dead"],
        fallback["foreign"],
        fallback["unreadable"],
    ) == (5, 1, 1, 2, 1)
    assert fallback["foreign_lanes"] == {OTHER_LANE: 1, "prod": 1}
    assert [(d["round_id"], d["persona_id"], d["title"], d["reason"]) for d in fallback["dead_letters"]] == [
        ("dead", "akao", "dead.txt", "rejected")
    ]
    assert own["carried"] == fallback["carried"] == 0
    assert own["depth_after"] == 1 and fallback["depth_after"] == 5
    assert (own["too_many"], fallback["too_many"]) == (False, False)
    assert await broker.depth(OWN) == 1
    assert await broker.depth(FALLBACK) == 5
    assert await read_record(participant=READING, limit=10) == []
    assert one_round.round_ids == []
    await _the_shared_dead_letter_was_never_taken(broker)


async def test_carrying_sends_each_unprocessed_round_to_the_reading_inbox_once(
    broker, reading_inbox  # noqa: F811
):
    one_round = await reading_inbox()
    await _seed_a_lane(broker)

    answer = await _carry(dry_run=False)

    queues = _by_queue(answer)
    assert queues[OWN]["carried"] == 1 and queues[OWN]["failed"] == 0
    assert queues[FALLBACK]["carried"] == 1 and queues[FALLBACK]["failed"] == 0
    rows = await read_record(participant=READING, limit=20)
    assert {(r["message_id"], r["sender"], r["outcome"]) for r in rows} == {
        ("pending", RESIDENT_NAMES["akao"], "sending"),
        ("pending", RESIDENT_NAMES["akao"], "delivered"),
        ("expired", RESIDENT_NAMES["akao"], "sending"),
        ("expired", RESIDENT_NAMES["akao"], "delivered"),
    }
    await eventually(lambda: _read(one_round, {"pending", "expired"}))
    # 本泳道的那条搬空了；不带后缀的那条剩下旧死信、别的泳道的、prod 的、解不开的。
    assert await broker.depth(OWN) == 0
    left = await _bodies(FALLBACK)
    assert sorted(_round_or_raw(b) for b in left) == sorted(
        ["dead", "other-lane", "prod-pending", "not json at all"]
    )

    # 再搬一次：没有新的可搬，读书收件箱也没有收到第二份。
    again = _by_queue(await _carry(dry_run=False))
    assert again[OWN]["unprocessed"] == 0 and again[FALLBACK]["unprocessed"] == 0

    # 丢掉旧死信：只有本泳道被拒收的那一条走了，读书收件箱没收到它。
    discarded = _by_queue(await _carry(dry_run=False, discard_dead=True))
    assert discarded[FALLBACK]["discarded_dead"] == 1
    left = await _bodies(FALLBACK)
    assert sorted(_round_or_raw(b) for b in left) == sorted(
        ["other-lane", "prod-pending", "not json at all"]
    )
    await asyncio.sleep(0.5)
    assert sorted(one_round.round_ids) == ["expired", "pending"]
    assert {r["message_id"] for r in await read_record(participant=READING, limit=20)} == {
        "pending",
        "expired",
    }
    await _the_shared_dead_letter_was_never_taken(broker)


async def _read(one_round: _Round, expected: set[str]) -> bool:
    return set(one_round.round_ids) == expected


def _round_or_raw(body: bytes) -> str:
    try:
        return json.loads(body)["round_id"]
    except ValueError:
        return body.decode()


async def test_a_round_of_someone_who_is_not_a_sister_is_put_back_as_failed(
    broker, reading_inbox  # noqa: F811
):
    one_round = await reading_inbox()
    await mq.declare_route(OLD, lane=LANE)
    await _publish_old(_body("stranger", persona_id="nobody"), lane=LANE)
    await eventually(lambda: _depth_is(broker, OWN, 1))

    own = _by_queue(await _carry(dry_run=False))[OWN]

    assert (own["unprocessed"], own["carried"], own["failed"]) == (1, 0, 1)
    assert await broker.depth(OWN) == 1
    assert await read_record(participant=READING, limit=10) == []
    assert one_round.round_ids == []


async def test_only_this_lanes_old_queue_is_deleted_and_only_once_it_is_empty(
    broker, reading_inbox  # noqa: F811
):
    await reading_inbox()
    await mq.declare_route(OLD, lane=None)
    await mq.declare_route(OLD, lane=LANE)
    await _publish_old(_body("stranger", persona_id="nobody"), lane=LANE)
    await eventually(lambda: _depth_is(broker, OWN, 1))

    # 本泳道的那条里还有一条放回去的：不删。不带后缀的那条在泳道里从来不删，空的也不删。
    kept = _by_queue(await _carry(dry_run=False, delete_empty=True))
    assert kept[OWN]["deleted"] is False and kept[FALLBACK]["deleted"] is False
    assert await broker.depth(OWN) == 1
    assert await broker.depth(FALLBACK) == 0

    # 只看的时候不删。
    channel = await mq._connection.channel()  # type: ignore[union-attr]
    queue = await channel.declare_queue(OWN, passive=True)
    await (await queue.get(no_ack=False, timeout=5)).ack()
    await channel.close()
    looked = _by_queue(await _carry(delete_empty=True))
    assert looked[OWN]["deleted"] is False
    assert await broker.depth(OWN) == 0

    gone = _by_queue(await _carry(dry_run=False, delete_empty=True))
    assert gone[OWN]["deleted"] is True and gone[FALLBACK]["deleted"] is False
    assert await broker.depth(OWN) == -1
    assert await broker.depth(FALLBACK) == 0


async def test_in_prod_the_one_old_queue_is_carried_and_deleted_and_the_shared_dead_letters_stay(
    broker, reading_inbox, monkeypatch  # noqa: F811
):
    monkeypatch.delenv("LANE")
    one_round = await reading_inbox()
    await mq.declare_route(OLD, lane=None)
    await _reject_once(None, _body("prod-dead", lane="prod"))
    await _publish_old(_body("prod-pending", lane="prod"), lane=None)
    await _publish_old(_body("ppe-pending", lane="ppe-x"), lane=None)
    await eventually(lambda: _depth_is(broker, FALLBACK, 2))

    looked = await _carry()
    assert looked["lane"] == "prod"
    (only,) = looked["queues"]
    assert only["queue"] == OLD.queue
    assert (only["unprocessed"], only["dead"], only["foreign"]) == (1, 0, 1)

    carried = _by_queue(await _carry(dry_run=False, discard_dead=True, delete_empty=True))[OLD.queue]
    assert (carried["carried"], carried["deleted"]) == (1, False)
    await eventually(lambda: _read(one_round, {"prod-pending"}))
    rows = await read_record(participant=READING, limit=10)
    assert {(r["message_id"], r["sender"]) for r in rows} == {("prod-pending", RESIDENT_NAMES["akao"])}
    assert [_round_or_raw(b) for b in await _bodies(OLD.queue)] == ["ppe-pending"]
    assert await broker.depth(DLQ_NAME) == 1

    channel = await mq._connection.channel()  # type: ignore[union-attr]
    queue = await channel.declare_queue(OLD.queue, passive=True)
    await (await queue.get(no_ack=False, timeout=5)).ack()
    await channel.close()
    gone = _by_queue(await _carry(dry_run=False, delete_empty=True))[OLD.queue]
    assert gone["deleted"] is True
    assert await broker.depth(OLD.queue) == -1
    await _the_shared_dead_letter_was_never_taken(broker)


async def test_old_queues_that_do_not_exist_are_reported_and_not_created(
    broker, reading_inbox  # noqa: F811
):
    await reading_inbox()

    for fields in ({}, {"dry_run": False, "discard_dead": True, "delete_empty": True}):
        queues = _by_queue(await _carry(**fields))
        assert list(queues) == [OWN, FALLBACK]
        assert all(q["exists"] is False and q["deleted"] is False for q in queues.values())

    assert await broker.depth(OWN) == -1
    assert await broker.depth(FALLBACK) == -1


async def test_one_call_carries_this_lanes_rounds_behind_any_number_of_foreign_ones(
    broker, reading_inbox  # noqa: F811
):
    """放回的消息回到队首：要是一次只扫前面一截，排在一大串别的泳道的消息后面的那几程，调多少次
    都搬不到。一次调用扫完整条队列，排在后面的也一次搬完。"""
    one_round = await reading_inbox()
    await mq.declare_route(OLD, lane=None)
    await mq.declare_route(OLD, lane=LANE)
    foreign = 1100  # 比原来一次最多扫的 1000 条还多
    for i in range(foreign):
        await _publish_old(_body(f"other-{i}", lane=OTHER_LANE), lane=None)
    await _publish_old(_body("behind-1"), lane=None)
    await _publish_old(_body("behind-2"), lane=None)
    await eventually(lambda: _depth_is(broker, FALLBACK, foreign + 2), timeout=30)

    fallback = _by_queue(await _carry(dry_run=False))[FALLBACK]

    assert (fallback["scanned"], fallback["foreign"], fallback["carried"]) == (
        foreign + 2,
        foreign,
        2,
    )
    assert fallback["too_many"] is False
    await eventually(lambda: _read(one_round, {"behind-1", "behind-2"}))
    # 放回的那一千多条要过一会儿才在 broker 上全部回到就绪（见 depth_after 的说明）。
    await eventually(lambda: _depth_is(broker, FALLBACK, foreign))


async def _never_taken(queue_name: str) -> list[str]:
    """队列里每一条的 round_id，并断言没有一条被谁取出来过（取出来再放回的 broker 会标成重投）。"""
    channel = await mq._connection.channel()  # type: ignore[union-attr]
    taken = []
    try:
        queue = await channel.declare_queue(queue_name, passive=True)
        while (incoming := await queue.get(no_ack=False, fail=False)) is not None:
            taken.append(incoming)
        assert [m.redelivered for m in taken] == [False] * len(taken)
        return [_round_or_raw(m.body) for m in taken]
    finally:
        for m in taken:
            await m.nack(requeue=True)
        await channel.close()


async def test_a_queue_deeper_than_the_scan_limit_is_reported_and_left_alone(
    broker, reading_inbox, monkeypatch  # noqa: F811
):
    """一次调用要把整条队列取出来、处理完才放回；队列深到超过上限，就一条都不取，只报深度和"太多"，
    看的时候也一样。"""
    from app.nodes import reading_carry_over

    monkeypatch.setattr(reading_carry_over, "SCAN_LIMIT", 3, raising=False)
    monkeypatch.delenv("LANE")
    one_round = await reading_inbox()
    await mq.declare_route(OLD, lane=None)
    for i in range(4):
        await _publish_old(_body(f"prod-{i}", lane="prod"), lane=None)
    await _publish_old(_body("ppe-pending", lane="ppe-x"), lane=None)
    await eventually(lambda: _depth_is(broker, OLD.queue, 5))

    for fields in (
        {},
        {"dry_run": False, "discard_dead": True, "discard_foreign": True, "delete_empty": True},
    ):
        (only,) = (await _carry(**fields))["queues"]
        assert (only["depth_before"], only.get("too_many"), only["scanned"]) == (5, True, 0), fields
        assert (only["carried"], only["deleted"], only["depth_after"]) == (0, False, 5), fields

    assert sorted(await _never_taken(OLD.queue)) == sorted(
        ["prod-0", "prod-1", "prod-2", "prod-3", "ppe-pending"]
    )
    assert await read_record(participant=READING, limit=10) == []
    assert one_round.round_ids == []


async def test_other_lanes_rounds_are_put_back_unless_they_are_to_be_discarded(
    broker, reading_inbox, monkeypatch, caplog  # noqa: F811
):
    """prod 不带后缀的那条旧队列里有 ppe 泳道过期转进来的消息，那些泳道已经没人消费。默认原样放回；
    人看过 dry_run（按泳道报了几条）之后给 ``discard_foreign``，就逐条把消息体写进日志、确认掉。
    解不开的不算别的泳道的，照样放回。"""
    monkeypatch.delenv("LANE")
    one_round = await reading_inbox()
    await mq.declare_route(OLD, lane=None)
    await _publish_old(_body("ppe-a-1", lane="ppe-a"), lane=None)
    await _publish_old(_body("prod-pending", lane="prod"), lane=None)
    await _publish_old(_body("ppe-b-1", lane="ppe-b"), lane=None)
    await _publish_old(_body("ppe-a-2", lane="ppe-a"), lane=None)
    await _publish_old(b"not json at all", lane=None)
    await eventually(lambda: _depth_is(broker, OLD.queue, 5))

    # 只看：给了 discard_foreign 也不丢。
    (looked,) = (await _carry(discard_foreign=True))["queues"]
    assert (looked["foreign"], looked["unreadable"], looked["discarded_foreign"]) == (3, 1, 0)
    assert looked["foreign_lanes"] == {"ppe-a": 2, "ppe-b": 1}
    assert await broker.depth(OLD.queue) == 5

    # 搬，不给 discard_foreign：本泳道的搬走，别的泳道的和解不开的都放回。
    (kept,) = (await _carry(dry_run=False))["queues"]
    assert (kept["carried"], kept["foreign"], kept["discarded_foreign"]) == (1, 3, 0)
    await eventually(lambda: _read(one_round, {"prod-pending"}))
    assert sorted(_round_or_raw(b) for b in await _bodies(OLD.queue)) == sorted(
        ["ppe-a-1", "ppe-b-1", "ppe-a-2", "not json at all"]
    )

    # 给 discard_foreign：别的泳道的逐条记下消息体、确认掉；解不开的还在。
    with caplog.at_level(logging.WARNING, logger="app.nodes.reading_carry_over"):
        (dropped,) = (await _carry(dry_run=False, discard_foreign=True))["queues"]
    assert (dropped["foreign"], dropped["discarded_foreign"], dropped["unreadable"]) == (3, 3, 1)
    assert [_round_or_raw(b) for b in await _bodies(OLD.queue)] == ["not json at all"]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for round_id, lane in (("ppe-a-1", "ppe-a"), ("ppe-b-1", "ppe-b"), ("ppe-a-2", "ppe-a")):
        assert json.dumps(_body(round_id, lane=lane)) in logged, logged
    await asyncio.sleep(0.5)
    assert one_round.round_ids == ["prod-pending"]

