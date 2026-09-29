"""失败路径：记录与投递之间出事、回答发不出去、两个消费者同时领到同一条、租约被接管后
旧处理者才返回、断线后提问、死信重放越过泳道。

全部跑在真 broker + 真 Postgres 上。故障是在真实调用链上注入的：broker 真的收下了消息，
然后让提交失败，或者让进程在那一刻"死掉"（抛一个不是 Exception 的异常，后面的代码
一行都不会执行，跟进程被杀一样）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from app.infra.rabbitmq import ISOLATED_DEAD_LETTERS, Route, mq
from app.messaging import receiving
from app.messaging import record as record_mod
from app.messaging.broker import inbox_route
from app.messaging.lifecycle import start_messaging, stop_messaging
from app.messaging.message import Kind, SendFailed, new_message
from app.messaging.receiving import inbox
from app.messaging.record import read_record
from app.messaging.sending import ask, send, send_at
from app.runtime.wire import RetryPolicy

from .conftest import LANE
from .helpers import Inbox, eventually, outcomes

pytestmark = pytest.mark.usefixtures("messaging_db")


class _Crash(BaseException):
    """进程在这一刻死了：不是 Exception，没有哪一层会接住它继续往下走。"""


@pytest.fixture
def broker_accepts(monkeypatch):
    """包住真实的 publish：broker 确认之后按需注入故障。"""
    state = {"accepted": 0, "crash_after_accept": False}
    real = mq.publish_with_confirm

    async def wrapped(*a, **kw):
        ok = await real(*a, **kw)
        if ok:
            state["accepted"] += 1
            if state["crash_after_accept"]:
                state["crash_after_accept"] = False
                raise _Crash()
        return ok

    monkeypatch.setattr(mq, "publish_with_confirm", wrapped)
    return state


@pytest.fixture
def commits_fail_after_accept(monkeypatch, broker_accepts):
    """broker 收下任何一条之后，记录者的每一次提交都失败。``state['on']=False`` 恢复。"""
    state = {"on": True}
    real = record_mod.get_session

    @asynccontextmanager
    async def flaky():
        async with real() as session:
            yield session
            if state["on"] and broker_accepts["accepted"]:
                raise RuntimeError("COMMIT failed")

    monkeypatch.setattr(record_mod, "get_session", flaky)
    return state


# ---------------------------------------------------------------------------
# 记录与投递
# ---------------------------------------------------------------------------


async def test_broker_accepts_then_the_record_commit_fails(broker, commits_fail_after_accept):
    """不能留下"已投递但没有任何记录"；调用方沿用同一个消息 id 重试，接收方只处理一次。"""
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()

    with pytest.raises(SendFailed) as failed:
        await send(sender="operator", recipient="world", body="提交会失败。")

    message_id = failed.value.message_id
    assert message_id
    commits_fail_after_accept["on"] = False
    assert outcomes(await read_record(message_id=message_id)) == ["sending"]

    retried = await send(
        sender="operator", recipient="world", body="提交会失败。", message_id=message_id
    )

    assert retried.delivered and retried.message_id == message_id
    await eventually(lambda: world.got)
    await asyncio.sleep(1.5)
    assert [m.message_id for m in world.got] == [message_id]
    assert outcomes(await read_record(message_id=message_id)) == [
        "sending",
        "sending",
        "delivered",
    ]


async def test_crash_between_broker_accepting_and_recording(broker, broker_accepts):
    """broker 收下之后进程马上死掉：记录里仍然看得出这条在发送途中；重启后沿用同一个
    消息 id 再发一次，接收方只处理一次。"""
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()

    broker_accepts["crash_after_accept"] = True
    with pytest.raises(_Crash):
        await send(sender="operator", recipient="world", body="发到一半进程没了。")

    (row,) = await read_record(participant="world")
    assert row["outcome"] == "sending"

    await send(
        sender="operator",
        recipient="world",
        body="发到一半进程没了。",
        message_id=row["message_id"],
    )
    await eventually(lambda: world.got)
    await asyncio.sleep(1.5)
    assert [m.message_id for m in world.got] == [row["message_id"]]


async def test_crash_right_after_scheduling_leaves_a_trace_and_retry_is_safe(
    broker, broker_accepts
):
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()
    at = datetime.now(UTC) + timedelta(seconds=2)

    broker_accepts["crash_after_accept"] = True
    with pytest.raises(_Crash):
        await send_at(sender="world", recipient="world", body="定时途中没了。", at=at)

    (row,) = await read_record(participant="world")
    assert row["outcome"] == "sending"

    await send_at(
        sender="world",
        recipient="world",
        body="定时途中没了。",
        at=at,
        message_id=row["message_id"],
    )
    await eventually(lambda: world.got, timeout=10)
    await asyncio.sleep(1.5)
    assert [m.message_id for m in world.got] == [row["message_id"]]


async def test_retrying_an_answered_question_with_its_id_gets_no_answer_and_no_second_run(
    broker, monkeypatch
):
    """问题已经被回答了，提问方却没能记下结果（ask 抛 SendFailed，原来那次等待已经撤掉）。
    沿用原 id 再问一次：接收方按 id 认出这个问题已经处理过，不再跑业务处理，提问方等到
    截止时刻拿到"没有回答"。回答不会被重放。"""
    from app.messaging import sending
    from app.messaging.record import Outcome

    real_record = sending.record
    broken = {"left": 1}

    async def question_result_row_fails_once(message, outcome, **kw):
        if message.kind is Kind.QUESTION and outcome is Outcome.DELIVERED and broken["left"]:
            broken["left"] -= 1
            raise SendFailed("COMMIT failed", message_id=message.message_id)
        await real_record(message, outcome, **kw)

    monkeypatch.setattr(sending, "record", question_result_row_fails_once)
    world = Inbox(answer="在。")
    inbox("world", on_message=world.on_message, on_question=world.on_question)
    await start_messaging()

    with pytest.raises(SendFailed) as failed:
        await ask(sender="operator", recipient="world", body="在吗？", timeout_seconds=10)
    question_id = failed.value.message_id
    await eventually(lambda: world.questions, timeout=5)

    retried = await ask(
        sender="operator",
        recipient="world",
        body="在吗？",
        timeout_seconds=2,
        message_id=question_id,
    )

    assert retried.question_id == question_id
    assert not retried.answered and retried.reason
    assert len(world.questions) == 1


# ---------------------------------------------------------------------------
# 问题在任何失败路径下都不重投
# ---------------------------------------------------------------------------


async def test_a_reply_that_cannot_be_published_does_not_bring_the_question_back(
    broker, monkeypatch
):
    monkeypatch.setattr(
        receiving,
        "PROCESSING_RETRY",
        RetryPolicy(n=4, backoff="linear", base_delay_ms=200, max_delay_ms=500, lease_ms=60_000),
    )
    real_publish = receiving.publish

    async def replies_fail(route, body, *, headers, delay_ms=None):
        if route.rk.startswith("messaging.reply."):
            raise SendFailed("reply queue unreachable")
        await real_publish(route, body, headers=headers, delay_ms=delay_ms)

    monkeypatch.setattr(receiving, "publish", replies_fail)
    world = Inbox(answer=None)
    inbox("world", on_message=world.on_message, on_question=world.on_question)
    await start_messaging()

    answer = await ask(sender="operator", recipient="world", body="在吗？", timeout_seconds=4)

    assert not answer.answered
    await asyncio.sleep(1.0)
    assert len(world.questions) == 1
    assert await broker.depth(f"{ISOLATED_DEAD_LETTERS}_{LANE}") == 0


@pytest.mark.parametrize("probe", ["claim", "mark_succeeded", "mark_failed", "undecodable"])
async def test_a_question_is_never_dead_lettered_even_when_its_bookkeeping_fails(
    broker, monkeypatch, probe
):
    """问题处理路径上的任何失败——包括去重状态本身读写失败、消息解不开——都只记一笔、
    确认掉：不重投，不进死信。走真的 broker 和真的确认流程，只把数据库那一步换成失败。"""
    import aio_pika

    from app.messaging.broker import headers as outbound_headers

    async def db_down(*a, **kw):
        raise RuntimeError("database is down")

    answer = "在。"
    if probe == "claim":
        monkeypatch.setattr(receiving, "claim_inflight", db_down)
    elif probe == "mark_succeeded":
        monkeypatch.setattr(receiving, "mark_succeeded", db_down)
    elif probe == "mark_failed":
        # 走到失败分支：回答发不出去，然后记失败这一步也失败
        answer = None
        real_publish = receiving.publish

        async def replies_fail(route, body, *, headers, delay_ms=None):
            if route.rk.startswith("messaging.reply."):
                raise SendFailed("reply queue unreachable")
            await real_publish(route, body, headers=headers, delay_ms=delay_ms)

        monkeypatch.setattr(receiving, "publish", replies_fail)
        monkeypatch.setattr(receiving, "mark_failed", db_down)
    world = Inbox(answer=answer)
    inbox("world", on_message=world.on_message, on_question=world.on_question)
    await start_messaging()

    if probe == "undecodable":
        question = new_message(
            sender="operator", recipient="world", body="在吗？", kind=Kind.QUESTION
        ).to_json()
        question["sender"] = "Not.A.Name"
        channel = await mq.open_channel()
        exchange = await channel.get_exchange("post_processing")
        await exchange.publish(
            aio_pika.Message(
                body=json.dumps(question).encode(),
                headers=outbound_headers(
                    {"x-reply-rk": "messaging.reply.nobody",
                     "x-answer-by": (datetime.now(UTC) + timedelta(seconds=30)).isoformat()}
                ),
            ),
            routing_key=f"inbox.world.{LANE}",
        )
        await channel.close()
    else:
        await ask(sender="operator", recipient="world", body="在吗？", timeout_seconds=3)

    await asyncio.sleep(1.5)
    assert await broker.depth(f"{ISOLATED_DEAD_LETTERS}_{LANE}") == 0
    assert await broker.depth(f"inbox_world_{LANE}") == 0
    assert len(world.questions) <= 1


# ---------------------------------------------------------------------------
# 去重状态按泳道分开；"没有送达"告知的 id 稳定
# ---------------------------------------------------------------------------


SHARED_ID = "shared0000000000000000000000000a"


async def test_two_lanes_sharing_a_database_each_process_the_same_message_id(
    broker, monkeypatch
):
    """ppe 和 prod 共用一个库：同一个消息 id 在一条泳道处理过，另一条泳道照样要处理。"""
    world = Inbox()
    inbox("world", on_message=world.on_message)

    monkeypatch.setenv("LANE", "prod")
    await start_messaging()
    await send(sender="operator", recipient="world", body="prod 的。", message_id=SHARED_ID)
    await eventually(lambda: len(world.got) == 1)
    await stop_messaging()

    monkeypatch.setenv("LANE", "ppe-review")
    await start_messaging()
    await send(sender="operator", recipient="world", body="ppe 的。", message_id=SHARED_ID)
    await eventually(lambda: len(world.got) == 2, timeout=5)

    assert [m.body for m in world.got] == ["prod 的。", "ppe 的。"]


async def test_two_lanes_sharing_a_database_each_deliver_the_same_scheduled_id(
    broker, monkeypatch
):
    world = Inbox()
    inbox("world", on_message=world.on_message)
    at = datetime.now(UTC) + timedelta(seconds=1)

    monkeypatch.setenv("LANE", "prod")
    await start_messaging()
    await send_at(sender="world", recipient="world", body="prod 定的。", at=at, message_id=SHARED_ID)
    await eventually(lambda: len(world.got) == 1, timeout=10)
    await stop_messaging()

    monkeypatch.setenv("LANE", "ppe-review")
    await start_messaging()
    await send_at(sender="world", recipient="world", body="ppe 定的。", at=at, message_id=SHARED_ID)
    await eventually(lambda: len(world.got) == 2, timeout=10)

    assert [m.body for m in world.got] == ["prod 定的。", "ppe 定的。"]


async def test_a_repeated_not_delivered_notice_keeps_its_id(broker, monkeypatch):
    """定时消息到点没送到：告知发出去了、结果行却没写成，定时那一步会重试并再发一次告知。
    两次告知必须是同一个 id，发送方只处理一次。"""
    from app.messaging import sending
    from app.messaging.record import Outcome

    monkeypatch.setattr(
        receiving,
        "PROCESSING_RETRY",
        RetryPolicy(n=4, backoff="linear", base_delay_ms=200, max_delay_ms=500, lease_ms=60_000),
    )
    real_record = sending.record
    broken = {"left": 1}

    async def notice_result_row_fails_once(message, outcome, **kw):
        if (
            message.kind is Kind.NOT_DELIVERED
            and outcome is Outcome.DELIVERED
            and broken["left"]
        ):
            broken["left"] -= 1
            raise SendFailed("COMMIT failed", message_id=message.message_id)
        await real_record(message, outcome, **kw)

    monkeypatch.setattr(sending, "record", notice_result_row_fails_once)
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()

    await send_at(
        sender="world",
        recipient="ayana",
        body="快递到了。",
        at=datetime.now(UTC) + timedelta(seconds=1),
    )

    await eventually(lambda: world.got, timeout=10)
    await asyncio.sleep(2.0)  # 重试那一次也跑完
    assert broken["left"] == 0, "故障没注入上，这个用例没测到重发"
    assert len(world.got) == 1
    notice_rows = [
        r for r in await read_record(participant="world") if r["kind"] == "not_delivered"
    ]
    assert len({r["message_id"] for r in notice_rows}) == 1
    assert [r["outcome"] for r in notice_rows].count("sending") == 2


# ---------------------------------------------------------------------------
# 并发与接管
# ---------------------------------------------------------------------------


async def test_two_consumers_taking_the_same_message_at_once_process_it_once(
    broker, monkeypatch, caplog
):
    monkeypatch.setattr(
        receiving,
        "PROCESSING_RETRY",
        RetryPolicy(n=4, backoff="linear", base_delay_ms=200, max_delay_ms=500, lease_ms=2_000),
    )
    caplog.set_level(logging.INFO, logger="app.messaging.receiving")
    calls: list[str] = []

    async def slow(message):
        calls.append(message.message_id)
        await asyncio.sleep(1.0)

    inbox("world", on_message=slow)
    await start_messaging()
    # 第二个消费者：同一条队列上另一个 channel，相当于另一个进程。
    await receiving._consume(
        inbox_route("world"), receiving._inbox_handler(receiving.INBOX_REGISTRY["world"])
    )

    message = new_message(sender="operator", recipient="world", body="同时领。", kind=Kind.MESSAGE)
    route = Route("inbox_world", "inbox.world", isolated=True)
    await asyncio.gather(
        mq.publish_with_confirm(route, message.to_json(), lane=LANE),
        mq.publish_with_confirm(route, message.to_json(), lane=LANE),
    )

    await asyncio.sleep(5.0)  # 两份都到过、被挡下的那份按租约重排后也回来过
    assert calls == [message.message_id]
    # 证明两份确实是同时在两个消费者手里：后到的那份撞上了还活着的租约。
    assert "is held by another worker; re-queued" in caplog.text


async def test_a_worker_that_lost_its_lease_cannot_overwrite_the_outcome(
    broker, monkeypatch, test_db
):
    """旧处理者超过租约还没返回，别人已接管并处理成功；旧处理者这时失败返回，不能把
    "已成功"改掉，也不能因此把消息再跑一遍。"""
    monkeypatch.setattr(
        receiving,
        "PROCESSING_RETRY",
        RetryPolicy(n=3, backoff="linear", base_delay_ms=200, max_delay_ms=500, lease_ms=1_000),
    )
    calls: list[int] = []

    async def first_slow_then_fast(message):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            await asyncio.sleep(3.0)
            raise RuntimeError("旧处理者超时后失败")

    inbox("world", on_message=first_slow_then_fast)
    await start_messaging()

    message = new_message(sender="operator", recipient="world", body="被接管。", kind=Kind.MESSAGE)
    route = Route("inbox_world", "inbox.world", isolated=True)
    await mq.publish_with_confirm(route, message.to_json(), lane=LANE)
    await asyncio.sleep(1.5)  # 租约已过期，旧处理者还在跑
    await mq.publish_with_confirm(route, message.to_json(), lane=LANE)  # broker 重投的那一份

    await asyncio.sleep(4.0)
    assert calls == [1, 2]
    async with test_db.begin() as conn:
        state = (
            await conn.execute(
                text(
                    "SELECT state FROM runtime_inflight "
                    "WHERE edge_id='inbox:world@coe-msg' AND idempotent_key=:k"
                ),
                {"k": message.message_id},
            )
        ).scalar()
    assert state == "succeeded"
    assert await broker.depth(f"{ISOLATED_DEAD_LETTERS}_{LANE}") == 0


async def test_ask_still_works_after_the_broker_connection_drops(broker, delayed_broker):
    world = Inbox(answer="在。")
    inbox("world", on_message=world.on_message, on_question=world.on_question)
    await start_messaging()
    assert (await ask(sender="operator", recipient="world", body="1", timeout_seconds=10)).answered

    _, (http_api, auth) = delayed_broker
    async with httpx.AsyncClient(base_url=http_api, auth=auth, timeout=10) as http:
        for _ in range(40):
            connections = (await http.get("/api/connections")).json()
            if connections:
                break
            await asyncio.sleep(0.5)
        assert connections
        dropped = {c["name"] for c in connections}
        for name in dropped:
            await http.delete(f"/api/connections/{name}")

    deadline = time.monotonic() + 30
    answered = False
    while time.monotonic() < deadline and not answered:
        await asyncio.sleep(1.0)
        try:
            answered = (
                await ask(sender="operator", recipient="world", body="2", timeout_seconds=3)
            ).answered
        except Exception:
            answered = False
    assert answered
    now_open: set[str] = set()
    async with httpx.AsyncClient(base_url=http_api, auth=auth, timeout=10) as http:
        for _ in range(40):  # management API 的连接列表按统计周期刷新
            now_open = {c["name"] for c in (await http.get("/api/connections")).json()}
            if now_open:
                break
            await asyncio.sleep(0.5)
    assert now_open and not (now_open & dropped), "回答走的是断线之后重建的连接"


# ---------------------------------------------------------------------------
# 死信：只能看、只能重放本部署泳道自己的
# ---------------------------------------------------------------------------


async def _dead_letter_one(broker, monkeypatch, lane: str) -> str:
    """在 ``lane`` 里让 world 的一条消息失败到底、进本泳道死信，返回消息 id。"""
    monkeypatch.setenv("LANE", lane)
    monkeypatch.setattr(
        receiving,
        "PROCESSING_RETRY",
        RetryPolicy(n=1, backoff="linear", base_delay_ms=100, max_delay_ms=100, lease_ms=60_000),
    )
    await start_messaging()
    delivery = await send(sender="operator", recipient="world", body=f"{lane} 的死信。")
    dead = ISOLATED_DEAD_LETTERS if lane == "prod" else f"{ISOLATED_DEAD_LETTERS}_{lane}"

    async def parked():
        return await broker.depth(dead) == 1

    await eventually(parked, timeout=10)
    await stop_messaging()
    return delivery.message_id


async def test_the_open_dlq_requeue_route_cannot_move_messaging_dead_letters(
    broker, monkeypatch
):
    """``/admin/dlq/requeue`` 没有凭据、也不看泳道：它不能碰通信机制的死信。在 ppe 部署上
    点名 prod 的死信队列，prod 的死信原地不动，prod 的收件箱什么都收不到。"""
    from app.nodes.dlq_admin import dlq_requeue_impl

    world = Inbox(fail_times=99)
    inbox("world", on_message=world.on_message)
    await _dead_letter_one(broker, monkeypatch, "prod")

    monkeypatch.setenv("LANE", "ppe-review")
    await dlq_requeue_impl({"queue": ISOLATED_DEAD_LETTERS, "limit": 10}, operator="t")

    assert await broker.depth(ISOLATED_DEAD_LETTERS) == 1
    assert await broker.depth("inbox_world") == 0


async def test_replay_takes_only_this_lanes_dead_letters_back_to_this_lanes_inboxes(
    broker, monkeypatch
):
    from app.messaging.dead_letters import replay_dead_letters

    world = Inbox(fail_times=2)
    inbox("world", on_message=world.on_message)
    await _dead_letter_one(broker, monkeypatch, "prod")
    ppe_id = await _dead_letter_one(broker, monkeypatch, "ppe-review")

    monkeypatch.setenv("LANE", "ppe-review")
    result = await replay_dead_letters(limit=10, operator="t")

    assert result["replayed"] == 1
    assert await broker.depth(f"{ISOLATED_DEAD_LETTERS}_ppe-review") == 0
    assert await broker.depth("inbox_world_ppe-review") == 1
    assert await broker.depth(ISOLATED_DEAD_LETTERS) == 1
    assert await broker.depth("inbox_world") == 0

    await start_messaging()
    await eventually(lambda: world.got, timeout=10)
    assert [m.message_id for m in world.got] == [ppe_id]


@pytest.mark.parametrize(
    "origin_queue,origin_rk",
    [
        ("inbox_world", "inbox.world"),  # 整个指向 prod
        ("inbox_world", "inbox.world.ppe-review"),  # routing key 像本泳道，队列却是 prod 的
        ("inbox_world_ppe-review", "inbox.world"),  # 队列像本泳道，routing key 却是 prod 的
        ("chat_response_lark_ppe-review", "chat.response.lark.ppe-review"),  # 本泳道，但不是通信机制的队列
    ],
)
async def test_replay_refuses_a_dead_letter_whose_origin_is_another_lane(
    broker, monkeypatch, origin_queue, origin_rk
):
    """死信头里写着的去处不是本泳道通信机制自己的队列：不发，原样留着。"""
    import aio_pika

    from app.messaging.dead_letters import replay_dead_letters

    monkeypatch.setenv("LANE", "prod")
    inbox("world", on_message=Inbox().on_message)
    await start_messaging()  # prod 的 world 收件箱存在，但没人该往里重放
    await stop_messaging()
    monkeypatch.setenv("LANE", "ppe-review")
    await start_messaging()  # 建出 ppe 的死信队列

    forged = new_message(sender="operator", recipient="world", body="伪造。", kind=Kind.MESSAGE)
    channel = await mq.open_channel()
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=json.dumps(forged.to_json()).encode(),
            headers={
                "x-death": [
                    {
                        "queue": origin_queue,
                        "routing-keys": [origin_rk],
                        "exchange": "post_processing",
                        "reason": "rejected",
                        "count": 1,
                    }
                ]
            },
        ),
        routing_key=f"{ISOLATED_DEAD_LETTERS}_ppe-review",
    )
    await channel.close()

    result = await replay_dead_letters(limit=10, operator="t")

    assert result["replayed"] == 0 and result["refused"] == 1
    assert await broker.depth("inbox_world") == 0
    assert await broker.depth(f"{ISOLATED_DEAD_LETTERS}_ppe-review") == 1


async def test_dead_letters_can_be_looked_at_without_taking_them_out(broker, monkeypatch):
    from app.messaging.dead_letters import peek_dead_letters

    world = Inbox(fail_times=99)
    inbox("world", on_message=world.on_message)
    message_id = await _dead_letter_one(broker, monkeypatch, LANE)

    seen = await peek_dead_letters(limit=10)

    assert [row["message"]["message_id"] for row in seen] == [message_id]
    assert seen[0]["origin"] == f"inbox_world_{LANE}"
    assert await broker.depth(f"{ISOLATED_DEAD_LETTERS}_{LANE}") == 1

