"""通信机制的契约，逐条对着 spec 决策 2、3 验，全部跑在真 broker + 真 Postgres 上。

每个用例开头一句话说它钉的是哪一条契约。进程的部署泳道是 ``coe-msg``（见 conftest），
个别用例会切到 prod 或别的泳道来验隔离。
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.infra.rabbitmq import DLQ_NAME, ISOLATED_DEAD_LETTERS, Route, mq
from app.messaging.lifecycle import start_messaging, stop_messaging
from app.messaging.message import Kind, Message, SendFailed, new_message
from app.messaging.receiving import inbox
from app.messaging.record import read_record
from app.messaging.sending import ask, send, send_at
from app.runtime.wire import RetryPolicy

from .conftest import LANE
from .helpers import Inbox, eventually, outcomes

pytestmark = pytest.mark.usefixtures("messaging_db")


# ---------------------------------------------------------------------------
# 发给具名参与者
# ---------------------------------------------------------------------------


async def test_send_reaches_an_open_inbox(broker):
    """发给某个具名参与者：送到它开设的收件箱，拥有者拿到的外层只有五样。"""
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()

    delivery = await send(
        sender="operator", recipient="world", body="赤尾在 18:02 走进了厨房。"
    )

    assert delivery.delivered and delivery.reason is None
    await eventually(lambda: world.got)
    m = world.got[0]
    assert (m.message_id, m.sender, m.recipient, m.kind, m.body) == (
        delivery.message_id,
        "operator",
        "world",
        Kind.MESSAGE,
        "赤尾在 18:02 走进了厨房。",
    )
    rows = await read_record(message_id=delivery.message_id)
    assert outcomes(rows) == ["sending", "delivered"]
    assert rows[0]["lane"] == LANE
    assert (rows[0]["sender"], rows[0]["recipient"], rows[0]["kind"]) == (
        "operator",
        "world",
        "message",
    )


async def test_send_to_an_inbox_nobody_opened(broker):
    """发往未开设的收件箱：不投递、照样记录、明确告诉发送方没有送达，也不替它建收件箱。"""
    await start_messaging()

    delivery = await send(sender="world", recipient="ayana", body="有人来找你。")

    assert not delivery.delivered
    assert delivery.reason
    rows = await read_record(message_id=delivery.message_id)
    assert outcomes(rows) == ["not_delivered"]
    assert rows[0]["reason"] == delivery.reason
    assert await broker.queue(f"inbox_ayana_{LANE}") is None


async def test_an_offline_owner_gets_its_messages_when_it_comes_back(broker):
    """收件箱开设之后，拥有者不在线时消息一直保留，上线再处理。"""
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()
    await stop_messaging()  # 开设过，然后下线

    delivery = await send(sender="operator", recipient="world", body="下雨了。")

    assert delivery.delivered
    assert await broker.depth(f"inbox_world_{LANE}") == 1
    await asyncio.sleep(0.5)
    assert world.got == []

    await start_messaging()
    await eventually(lambda: world.got)
    assert world.got[0].message_id == delivery.message_id


# ---------------------------------------------------------------------------
# 提问并同步拿到回答
# ---------------------------------------------------------------------------


async def test_ask_returns_the_owners_answer(broker):
    """向具名参与者提问并同步拿到回答；回答本身也是一条被记录的消息。"""
    world = Inbox(answer="厨房里灯亮着，水壶在响。")
    inbox("world", on_message=world.on_message, on_question=world.on_question)
    await start_messaging()

    answer = await ask(
        sender="operator", recipient="world", body="厨房现在什么样？", timeout_seconds=10
    )

    assert answer.answered and answer.text == "厨房里灯亮着，水壶在响。"
    assert world.questions[0].kind == Kind.QUESTION
    assert world.questions[0].message_id == answer.question_id
    # 回答方在回答发出、broker 确认之后才写最后一行，可能比提问方拿到回答稍晚；
    # 两边的行也会交错，所以按类型分开看各自的先后。
    async def both_complete():
        rows = await read_record(message_id=answer.question_id)
        return len(rows) == 4

    await eventually(both_complete, timeout=5)
    rows = await read_record(message_id=answer.question_id)
    assert [r["outcome"] for r in rows if r["kind"] == "question"] == [
        "sending",
        "delivered",
    ]
    assert [r["outcome"] for r in rows if r["kind"] == "answer"] == [
        "sending",
        "delivered",
    ]
    reply = [r for r in rows if r["kind"] == "answer"][-1]
    assert reply["in_reply_to"] == answer.question_id
    assert (reply["sender"], reply["recipient"], reply["body"]) == (
        "world",
        "operator",
        "厨房里灯亮着，水壶在响。",
    )


async def test_ask_an_inbox_nobody_opened_is_answered_with_no_answer_at_once(broker):
    await start_messaging()

    started = time.monotonic()
    answer = await ask(
        sender="operator", recipient="ayana", body="你在哪？", timeout_seconds=30
    )

    assert not answer.answered and answer.text is None and answer.reason
    assert time.monotonic() - started < 5
    assert outcomes(await read_record(message_id=answer.question_id)) == [
        "not_delivered"
    ]


async def test_ask_an_offline_owner_gives_no_answer_and_is_not_retried(broker):
    """对方不在线：等到超时拿到"没有回答"；问题只发一次，对方回来后也不会再去答一个没人等的问题。"""
    world = Inbox(answer="在。")
    inbox("world", on_message=world.on_message, on_question=world.on_question)
    await start_messaging()
    await stop_messaging()

    started = time.monotonic()
    answer = await ask(sender="operator", recipient="world", body="在吗？", timeout_seconds=1.5)

    assert not answer.answered and answer.reason
    assert time.monotonic() - started >= 1.5
    assert outcomes(await read_record(message_id=answer.question_id)) == [
        "sending",
        "delivered",
        "no_answer",
    ]
    assert await broker.depth(f"inbox_world_{LANE}") == 1

    await start_messaging()
    await asyncio.sleep(1.5)
    assert world.questions == []
    assert await broker.depth(f"inbox_world_{LANE}") == 0


async def test_ask_when_the_owner_fails_gives_no_answer_promptly_without_retry(broker):
    world = Inbox(answer=RuntimeError("应答时出错"))
    inbox("world", on_message=world.on_message, on_question=world.on_question)
    await start_messaging()

    started = time.monotonic()
    answer = await ask(sender="operator", recipient="world", body="谁在家？", timeout_seconds=20)

    assert not answer.answered and answer.reason
    assert time.monotonic() - started < 10
    await asyncio.sleep(1.5)
    assert len(world.questions) == 1
    assert outcomes(await read_record(message_id=answer.question_id)) == [
        "sending",
        "delivered",
        "no_answer",
    ]


async def test_an_answer_that_cannot_be_recorded_is_not_sent_and_not_retried(
    broker, monkeypatch
):
    """回答也是一条消息，记录失败它就不发；提问方立刻拿到"没有回答"，问题不重投。"""
    from app.messaging import receiving

    async def recorder_down(*a, **kw):
        raise SendFailed("recorder is down")

    monkeypatch.setattr(receiving, "publish_recorded", recorder_down)
    world = Inbox(answer="在。")
    inbox("world", on_message=world.on_message, on_question=world.on_question)
    await start_messaging()

    started = time.monotonic()
    answer = await ask(sender="operator", recipient="world", body="在吗？", timeout_seconds=20)

    assert not answer.answered and answer.reason
    assert time.monotonic() - started < 10
    await asyncio.sleep(1.5)
    assert len(world.questions) == 1


async def test_ask_when_the_owner_has_nothing_to_say(broker):
    world = Inbox(answer=None)
    inbox("world", on_message=world.on_message, on_question=world.on_question)
    await start_messaging()

    answer = await ask(sender="operator", recipient="world", body="?", timeout_seconds=10)

    assert not answer.answered and answer.reason


async def test_ask_an_inbox_that_takes_no_questions(broker):
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()

    answer = await ask(sender="operator", recipient="world", body="?", timeout_seconds=10)

    assert not answer.answered and answer.reason


# ---------------------------------------------------------------------------
# 指定时刻送达
# ---------------------------------------------------------------------------


async def test_send_at_delivers_at_the_designated_time_even_to_oneself(broker):
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()

    at = datetime.now(UTC) + timedelta(seconds=2)
    message_id = await send_at(sender="world", recipient="world", body="该醒了。", at=at)

    await asyncio.sleep(1.0)
    assert world.got == []
    await eventually(lambda: world.got, timeout=10)
    m = world.got[0]
    assert (m.message_id, m.sender, m.recipient, m.time) == (message_id, "world", "world", at)
    assert world.received_at[0] >= at
    rows = await read_record(message_id=message_id)
    assert outcomes(rows) == ["sending", "scheduled", "sending", "delivered"]
    assert rows[1]["recorded_at"] < at <= rows[3]["recorded_at"]


async def test_send_at_decides_about_the_inbox_when_it_is_due(broker):
    """定时消息在送达那一刻判断收件箱是否存在：排的时候没开设、到点前开设了，照样送到。"""
    await start_messaging()
    at = datetime.now(UTC) + timedelta(seconds=2.5)
    message_id = await send_at(sender="world", recipient="ayana", body="快递到了。", at=at)
    assert outcomes(await read_record(message_id=message_id)) == ["sending", "scheduled"]

    await stop_messaging()
    ayana = Inbox()
    inbox("ayana", on_message=ayana.on_message)
    await start_messaging()

    await eventually(lambda: ayana.got, timeout=10)
    assert outcomes(await read_record(message_id=message_id)) == [
        "sending",
        "scheduled",
        "sending",
        "delivered",
    ]


async def test_send_at_to_an_inbox_still_missing_when_due_tells_the_sender(broker):
    """到点时收件箱仍不存在：记录没有送达，并给发送方发一条"没有送达"。"""
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()

    at = datetime.now(UTC) + timedelta(seconds=1.5)
    message_id = await send_at(sender="world", recipient="ayana", body="快递到了。", at=at)

    await eventually(lambda: world.got, timeout=10)
    notice = world.got[0]
    assert notice.kind == Kind.NOT_DELIVERED
    assert notice.sender == "ayana" and notice.recipient == "world"
    assert "快递到了。" in notice.body
    assert outcomes(await read_record(message_id=message_id)) == [
        "sending",
        "scheduled",
        "not_delivered",
    ]


async def test_send_at_beyond_the_broker_delay_limit_is_split_transparently(
    broker, monkeypatch
):
    """平台底层延时的上限由机制自己处理：把上限压到 0.6 秒，排 2 秒后照样在 2 秒后到。"""
    from app.messaging import broker as messaging_broker

    monkeypatch.setattr(messaging_broker, "DELAY_LIMIT_MS", 600)
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()

    at = datetime.now(UTC) + timedelta(seconds=2)
    await send_at(sender="world", recipient="world", body="分段送达。", at=at)

    await eventually(lambda: world.got, timeout=10)
    assert world.received_at[0] >= at


# ---------------------------------------------------------------------------
# 至少一次、按消息 id 去重、失败有限次重试后进人工可重放的位置
# ---------------------------------------------------------------------------


async def test_the_same_message_delivered_twice_is_handled_once(broker):
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()

    delivery = await send(sender="operator", recipient="world", body="一次。")
    await eventually(lambda: world.got)
    duplicate = (await read_record(message_id=delivery.message_id))[0]
    body = Message(
        message_id=delivery.message_id,
        sender="operator",
        recipient="world",
        time=duplicate["message_time"],
        kind=Kind.MESSAGE,
        body="一次。",
    ).to_json()
    assert await mq.publish_with_confirm(
        Route("inbox_world", "inbox.world", isolated=True), body, lane=LANE
    )

    await asyncio.sleep(1.5)
    assert len(world.got) == 1


async def test_a_failing_message_is_retried_then_parked_in_its_lanes_dead_letters(
    broker, monkeypatch
):
    from app.messaging import receiving
    from app.messaging.dead_letters import replay_dead_letters

    monkeypatch.setattr(
        receiving,
        "PROCESSING_RETRY",
        RetryPolicy(n=3, backoff="linear", base_delay_ms=200, max_delay_ms=500, lease_ms=60_000),
    )
    world = Inbox(fail_times=3)
    inbox("world", on_message=world.on_message)
    await start_messaging()

    delivery = await send(sender="operator", recipient="world", body="会失败三次。")

    dead_letters = f"{ISOLATED_DEAD_LETTERS}_{LANE}"
    await eventually(lambda: broker.depth(dead_letters), timeout=15)
    assert await broker.depth(dead_letters) == 1
    assert world.calls == 3
    assert world.got == []
    assert await broker.queue(DLQ_NAME) is None or await broker.depth(DLQ_NAME) == 0

    result = await replay_dead_letters(limit=10, operator="test")

    assert result == {"replayed": 1, "refused": 0, "failed": 0}
    await eventually(lambda: world.got, timeout=10)
    assert world.got[0].message_id == delivery.message_id
    assert await broker.depth(dead_letters) == 0


async def test_a_message_held_by_a_crashed_peer_is_processed_after_its_lease(
    broker, test_db
):
    """别的进程拿着它处理到一半死了：这条不能被当成重复丢掉，租约过期后照样处理。"""
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()

    message = new_message(
        sender="operator", recipient="world", body="半路接管。", kind=Kind.MESSAGE
    )
    async with test_db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runtime_inflight (edge_id, idempotent_key, data_table, "
                "state, attempts, locked_until, worker_id) VALUES "
                "('inbox:world@coe-msg', :k, 'inbox_world', 'processing', 1, "
                "now() + interval '1.5 seconds', 'dead-peer:1')"
            ),
            {"k": message.message_id},
        )
    assert await mq.publish_with_confirm(
        Route("inbox_world", "inbox.world", isolated=True), message.to_json(), lane=LANE
    )

    await asyncio.sleep(0.8)
    assert world.got == []
    await eventually(lambda: world.got, timeout=10)
    assert len(world.got) == 1


# ---------------------------------------------------------------------------
# 记录是发送的一部分
# ---------------------------------------------------------------------------


async def test_when_recording_fails_the_send_fails_and_nothing_is_delivered(
    broker, test_db
):
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()
    async with test_db.begin() as conn:
        await conn.execute(text("DROP TABLE message_record"))

    with pytest.raises(SendFailed):
        await send(sender="operator", recipient="world", body="没记下来就不算发出。")
    with pytest.raises(SendFailed):
        await send_at(
            sender="world",
            recipient="world",
            body="没记下来就不算排上。",
            at=datetime.now(UTC) + timedelta(seconds=1),
        )

    await asyncio.sleep(2.0)
    assert world.got == []
    assert await broker.depth(f"inbox_world_{LANE}") == 0
    assert await broker.depth(f"messaging_scheduled_{LANE}") == 0


async def test_when_the_broker_does_not_confirm_the_send_fails_and_the_record_says_so(
    broker, monkeypatch
):
    """broker 没确认：发送失败，记录里是"交出去了、没确认"，而不是"送到了"。"""
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()

    async def refuse(*a, **kw):
        return False

    monkeypatch.setattr(mq, "publish_with_confirm", refuse)
    with pytest.raises(SendFailed) as failed:
        await send(sender="operator", recipient="world", body="broker 没确认。")

    rows = await read_record(participant="world")
    assert outcomes(rows) == ["sending", "unconfirmed"]
    assert {r["message_id"] for r in rows} == {failed.value.message_id}


# ---------------------------------------------------------------------------
# 按泳道隔离：不退回 prod、闲置不过期、死信不共用
# ---------------------------------------------------------------------------


async def test_inbox_queues_never_fall_back_never_expire_and_dead_letter_per_lane(broker):
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()

    args = (await broker.queue(f"inbox_world_{LANE}"))["arguments"]
    assert "x-message-ttl" not in args
    assert "x-expires" not in args
    assert args["x-dead-letter-exchange"] == ""
    assert args["x-dead-letter-routing-key"] == f"{ISOLATED_DEAD_LETTERS}_{LANE}"
    scheduled = (await broker.queue(f"messaging_scheduled_{LANE}"))["arguments"]
    assert "x-message-ttl" not in scheduled and "x-expires" not in scheduled


@pytest.mark.slow
async def test_a_lane_message_without_a_consumer_never_shows_up_in_prods_inbox(
    broker, monkeypatch
):
    """泳道收件箱没人消费时，过了平台那 10 秒回退窗口也不会跑到 prod 的同名收件箱里。"""
    prod_world = Inbox()
    inbox("world", on_message=prod_world.on_message)
    monkeypatch.setenv("LANE", "prod")
    await start_messaging()  # prod 的 world 在线

    monkeypatch.setenv("LANE", LANE)
    await start_messaging()
    await stop_messaging()  # 泳道的 world 开设过、下线
    monkeypatch.setenv("LANE", "prod")
    await start_messaging()
    monkeypatch.setenv("LANE", LANE)

    delivery = await send(sender="operator", recipient="world", body="泳道里的事。")
    assert delivery.delivered

    await asyncio.sleep(11.5)
    assert prod_world.got == []
    assert await broker.depth(f"inbox_world_{LANE}") == 1


async def test_a_lane_only_reaches_inboxes_opened_in_that_lane(broker, monkeypatch):
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()

    monkeypatch.setenv("LANE", "coe-other")
    delivery = await send(sender="operator", recipient="world", body="别的泳道。")

    assert not delivery.delivered
    await asyncio.sleep(0.5)
    assert world.got == []
    rows = await read_record(message_id=delivery.message_id)
    assert rows[0]["lane"] == "coe-other"


async def test_the_recorder_keeps_messages_apart_by_lane(broker, monkeypatch):
    await start_messaging()
    here = await send(sender="operator", recipient="nobody", body="这条泳道。")
    monkeypatch.setenv("LANE", "coe-other")
    there = await send(sender="operator", recipient="nobody", body="那条泳道。")

    assert [r["message_id"] for r in await read_record(participant="nobody")] == [
        there.message_id
    ]
    monkeypatch.setenv("LANE", LANE)
    assert [r["message_id"] for r in await read_record(participant="nobody")] == [
        here.message_id
    ]


async def test_the_runtime_migration_creates_the_record_table(test_db, monkeypatch):
    """记录者的表跟 runtime_inflight 一样由运行时自己建：每个 App 启动时都会建。

    Data 注册表清空：这里只看运行时自己的那几张表，别的测试顺带注册进来的 Data 类
    跟这件事无关。
    """
    import app.runtime.engine as engine_mod
    from app.runtime.engine import Runtime

    monkeypatch.setattr(engine_mod, "DATA_REGISTRY", set())
    async with test_db.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS message_record"))
    await Runtime(app_name="world", migrate_schema_on_run=False).migrate_schema()

    async with test_db.begin() as conn:
        exists = (
            await conn.execute(text("SELECT to_regclass('public.message_record')"))
        ).scalar()
    assert exists == "message_record"
