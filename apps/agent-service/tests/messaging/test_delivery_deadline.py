"""一次投递从领取、处理到记下结果，整体有一个比 broker 确认时限短的期限（2026-10-07，T9 决定 7）。

领取和记结果那十几次查库在处理时限之外；库很慢时，它们按各自的上限加起来能超过 broker 等确认的
30 分钟，broker 就关掉整个通道，同一通道上别的投递跟着被取消。现在一次投递到了期限
（:data:`app.messaging.receiving.DELIVERY_DEADLINE`）就取消、放开占位，只把这一条交还 broker 重投，
通道照常开着。开设收件箱时检查处理时限给领取和记结果留够了余量。

"库很慢"是一把咨询锁挡在数据库里的一次查询，只挡着一条投递；进程的连接走
:mod:`tests.data.db_proxy`，被期限取消的那次查询收尾时等不到数据库关连接的消息，靠决定 6 的宽限期
出来。跑在真 broker + 真 Postgres 上，期限缩短到几秒。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.data import dialect
from app.data.session import get_session
from app.messaging import receiving
from app.messaging.lifecycle import start_messaging
from app.messaging.receiving import inbox
from app.messaging.sending import send
from tests.data.db_proxy import process_db_behind

from .conftest import LANE
from .helpers import HangsOnce, Inbox, eventually

pytestmark = pytest.mark.usefixtures("messaging_db")

DEADLINE = 3.0
GRACE = 0.5
HELD_UP = "卡在库里。"
SLOW = "慢慢来。"
LOCK_KEY = 42


@pytest.fixture
async def process_db(test_db, test_db_dsn, monkeypatch):
    monkeypatch.setattr(dialect, "TERMINATE_GRACE_SECONDS", GRACE)
    async with process_db_behind(test_db_dsn, monkeypatch) as (_proxy, engine):
        yield engine


async def _inflight(test_db, message_id: str) -> dict:
    async with test_db.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT state, attempts, last_error FROM runtime_inflight "
                "WHERE edge_id = :e AND idempotent_key = :k"
            ),
            {"e": f"inbox:world@{LANE}", "k": message_id},
        )
        return dict(row.mappings().one())


async def test_a_delivery_held_up_in_the_database_is_handed_back_at_its_deadline_alone(
    process_db, broker, test_db, monkeypatch, caplog
):
    monkeypatch.setattr(receiving, "DELIVERY_DEADLINE", timedelta(seconds=DEADLINE))
    monkeypatch.setattr(receiving, "PUT_BACK_BASE_SECONDS", 0.1)
    caplog.set_level(logging.INFO, logger="app.messaging.receiving")
    attempts: dict[str, int] = {}
    finished: list[str] = []
    cancelled: list[str] = []
    entered = asyncio.Event()

    async def handler(message) -> None:
        attempts[message.body] = attempts.get(message.body, 0) + 1
        try:
            if message.body == HELD_UP and attempts[HELD_UP] == 1:
                entered.set()
                async with get_session() as s:
                    await s.execute(text(f"SELECT pg_advisory_xact_lock({LOCK_KEY})"))
            elif message.body == SLOW:
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            cancelled.append(message.body)
            raise
        finished.append(message.body)

    inbox("world", on_message=handler)
    await start_messaging()
    async with test_db.connect() as holder:
        await holder.execute(text(f"SELECT pg_advisory_lock({LOCK_KEY})"))
        try:
            held_up = await send(sender="operator", recipient="world", body=HELD_UP)
            await asyncio.wait_for(entered.wait(), timeout=10)
            # 它离期限还有一秒时，同一通道上来了另一条，要跑两秒：交还那一条时它正跑着。
            await asyncio.sleep(DEADLINE - 1.0)
            slow = await send(sender="operator", recipient="world", body=SLOW)

            # 锁一直没放：重投的那一份不再查那把锁，它能跑完，只能是期限到了被交还、重投了。
            await eventually(lambda: HELD_UP in finished, timeout=DEADLINE + GRACE + 3)
            await eventually(lambda: SLOW in finished, timeout=5)
        finally:
            await holder.execute(text(f"SELECT pg_advisory_unlock({LOCK_KEY})"))

    assert attempts == {HELD_UP: 2, SLOW: 1}
    assert cancelled == [HELD_UP], "交还那一条时，同一通道上别的投递被取消了"
    first = await _inflight(test_db, held_up.message_id)
    assert (first["state"], first["attempts"]) == ("succeeded", 2)
    assert "deadline" in first["last_error"], first["last_error"]
    second = await _inflight(test_db, slow.message_id)
    assert (second["state"], second["attempts"]) == ("succeeded", 1)
    assert "was closed" not in caplog.text, "通道被关掉了"
    await eventually(lambda: _empty(broker), timeout=5)


async def test_a_delivery_whose_deadline_falls_after_its_success_was_recorded_stays_succeeded(
    broker, test_db, monkeypatch
):
    """标记成功那一笔已经落下、还没返回（比如卡在会话退出时关连接）时到了期限：放开占位不能把
    "成功"改掉，交还重投的那一份在领取时就被挡掉，处理函数只看到它一次。"""
    monkeypatch.setattr(receiving, "DELIVERY_DEADLINE", timedelta(seconds=1))
    monkeypatch.setattr(receiving, "PUT_BACK_BASE_SECONDS", 0.1)
    mark = HangsOnce(receiving.mark_succeeded, after_commit=True)
    monkeypatch.setattr(receiving, "mark_succeeded", mark)
    claims: list[str] = []
    real_claim = receiving.claim_inflight

    async def counted_claim(**kw):
        claims.append(kw["worker_id"])
        return await real_claim(**kw)

    monkeypatch.setattr(receiving, "claim_inflight", counted_claim)
    world = Inbox()
    inbox("world", on_message=world.on_message)
    await start_messaging()
    delivery = await send(sender="operator", recipient="world", body="成功记下之后卡住。")

    await eventually(lambda: len(claims) == 2, timeout=5)  # 交还重投的那一份来领过了
    await asyncio.sleep(0.5)

    assert [m.message_id for m in world.got] == [delivery.message_id]
    row = await _inflight(test_db, delivery.message_id)
    assert row["state"] == "succeeded"
    assert row["last_error"] is None, "到期放开占位改了一条已经成功的记录"
    assert await _empty(broker)


async def _empty(broker) -> bool:
    return await broker.depth(f"inbox_world_{LANE}") == 0


def test_an_inbox_whose_processing_timeout_leaves_no_room_for_claiming_and_settling_is_refused():
    fits = receiving.DELIVERY_DEADLINE - receiving.CLAIM_AND_SETTLE_ROOM

    with pytest.raises(ValueError, match="deadline"):
        inbox("world", on_message=Inbox().on_message, processing_timeout=fits + timedelta(seconds=1))
    inbox("world", on_message=Inbox().on_message, processing_timeout=fits)


def test_the_delivery_deadline_leaves_the_broker_room_for_what_comes_after_it():
    """期限到了之后还要做的都落在余量里：被取消那一步的收尾、放开占位、放回之前停的那一会儿。"""
    from app.infra.rabbitmq import BROKER_ACK_TIMEOUT_MS

    after = timedelta(
        seconds=dialect.TERMINATE_GRACE_SECONDS
        + receiving.RELEASE_SECONDS
        + dialect.TERMINATE_GRACE_SECONDS
        + receiving.PUT_BACK_CAP_SECONDS
    )
    assert receiving.DELIVERY_DEADLINE + after < timedelta(milliseconds=BROKER_ACK_TIMEOUT_MS)
