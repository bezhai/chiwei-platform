"""一次投递在领取或标记的时候被取消：占位立刻放开，重投的那一份不用等租约过期（2026-10-07）。

10-06 在 coe-world 上，几次投递卡在领取那一步（事务已经提交，会话退出时关连接一直等着），后来
broker 关掉通道、把它们取消了。可"被取消就放开占位"只包着处理函数那一段，这几条的占位一直是
``processing``，重投的那一份等了一个小时租约过期才有人接。

取消有两个来源：进程在停（:func:`app.messaging.receiving.stop_receiving`），和投递所在的通道被
关掉（broker 关的，或者连接断了）。两种都要放开占位，记下来的原因要分得开——10-06 那次 broker
关通道取消的投递，记的是"进程正在停止时被取消"。

卡住是在真实调用链上注入的：领取照常提交，然后不返回；标记成功在提交之前不返回。通道是让 broker
自己关的：在消费通道上确认一个不存在的投递编号，broker 按协议关掉这个通道。跑在真 broker + 真
Postgres 上。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.messaging import receiving
from app.messaging.lifecycle import start_messaging, stop_messaging
from app.messaging.receiving import inbox
from app.messaging.sending import ask, send
from app.runtime.inflight import claim_inflight
from app.runtime.retry import RetryPolicy

from .conftest import LANE
from .helpers import HangsOnce, Inbox, eventually

pytestmark = pytest.mark.usefixtures("messaging_db")

# 处理时限 10 分钟 → 占位租约 11 分钟：占位不放开的话，重投的那一份在测试里等不到。
LONG_LEASE = timedelta(minutes=10)


async def _close_the_inbox_channel_from_the_broker(queue: str = f"inbox_world_{LANE}") -> None:
    """让 broker 关掉 world 收件箱（或别的队列）的消费通道：确认一个这条通道上不存在的投递编号。"""
    (channel,) = [ch for ch, q, _tag in receiving._consumers if q.name == queue]
    underlay = await channel.get_underlay_channel()
    await underlay.basic_ack(delivery_tag=999_999)


async def _inflight(test_db, message_id: str) -> dict:
    async with test_db.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT state, attempts, last_error, worker_id FROM runtime_inflight "
                "WHERE edge_id = :e AND idempotent_key = :k"
            ),
            {"e": f"inbox:world@{LANE}", "k": message_id},
        )
        return dict(row.mappings().one())


async def test_a_delivery_cancelled_while_claiming_because_its_channel_closed_is_taken_up_at_once(
    broker, test_db, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="app.messaging.receiving")
    claim = HangsOnce(receiving.claim_inflight, after_commit=True)
    monkeypatch.setattr(receiving, "claim_inflight", claim)
    world = Inbox()
    inbox("world", on_message=world.on_message, processing_timeout=LONG_LEASE)
    await start_messaging()
    delivery = await send(sender="operator", recipient="world", body="领取时卡住。")
    await asyncio.wait_for(claim.stuck.wait(), timeout=10)

    await _close_the_inbox_channel_from_the_broker()

    # broker 把没确认的这条重投到重新打开的通道上：占位放开了，马上有人接。
    await eventually(lambda: world.got, timeout=10)
    assert [m.message_id for m in world.got] == [delivery.message_id]
    row = await _inflight(test_db, delivery.message_id)
    assert row["state"] == "succeeded"
    assert row["attempts"] == 2
    assert "channel" in row["last_error"], row["last_error"]
    assert "process was stopping" not in row["last_error"], "通道被关掉的取消记成了进程在停"
    assert "PRECONDITION_FAILED" in caplog.text, "broker 关通道的原因没有记进日志"


async def test_a_delivery_cancelled_while_claiming_because_the_process_stops_is_released(
    broker, test_db, monkeypatch
):
    monkeypatch.setattr(receiving, "STOP_GRACE_SECONDS", 0.3)
    claim = HangsOnce(receiving.claim_inflight, after_commit=True)
    monkeypatch.setattr(receiving, "claim_inflight", claim)
    inbox("world", on_message=Inbox().on_message, processing_timeout=LONG_LEASE)
    await start_messaging()
    delivery = await send(sender="operator", recipient="world", body="领取时卡住。")
    await asyncio.wait_for(claim.stuck.wait(), timeout=10)

    await asyncio.wait_for(stop_messaging(), timeout=10)

    assert await broker.depth(f"inbox_world_{LANE}") == 1, "被取消的投递没有放回收件箱"
    row = await _inflight(test_db, delivery.message_id)
    assert row["state"] != "processing", "占位没放开，重投的那一份要等租约过期"
    assert "process was stopping" in row["last_error"], row["last_error"]


async def test_a_delivery_cancelled_while_marking_its_success_is_taken_up_at_once(
    broker, test_db, monkeypatch
):
    """标记成功那一笔还没落下就被取消：放开占位，重投的那一份再交给处理函数一次（至少一次）。"""
    mark = HangsOnce(receiving.mark_succeeded, after_commit=False)
    monkeypatch.setattr(receiving, "mark_succeeded", mark)
    world = Inbox()
    inbox("world", on_message=world.on_message, processing_timeout=LONG_LEASE)
    await start_messaging()
    delivery = await send(sender="operator", recipient="world", body="标记时卡住。")
    await asyncio.wait_for(mark.stuck.wait(), timeout=10)

    await _close_the_inbox_channel_from_the_broker()

    await eventually(lambda: len(world.got) == 2, timeout=10)
    assert [m.message_id for m in world.got] == [delivery.message_id] * 2
    await eventually(
        lambda: _state_is(test_db, delivery.message_id, "succeeded"), timeout=10
    )


async def _state_is(test_db, message_id: str, state: str) -> bool:
    return (await _inflight(test_db, message_id))["state"] == state


async def test_a_delivery_cancelled_after_its_success_was_recorded_stays_succeeded(
    broker, test_db, monkeypatch
):
    """标记成功那一笔已经落下、之后才被取消（比如卡在关连接上）：放开占位不能把"成功"改掉。重投的
    那一份在领取时就被挡掉，处理函数只看到它一次。"""
    mark = HangsOnce(receiving.mark_succeeded, after_commit=True)
    monkeypatch.setattr(receiving, "mark_succeeded", mark)
    claims: list[str] = []
    real_claim = receiving.claim_inflight

    async def counted_claim(**kw):
        claims.append(kw["worker_id"])
        return await real_claim(**kw)

    monkeypatch.setattr(receiving, "claim_inflight", counted_claim)
    world = Inbox()
    inbox("world", on_message=world.on_message, processing_timeout=LONG_LEASE)
    await start_messaging()
    delivery = await send(sender="operator", recipient="world", body="成功记下之后卡住。")
    await asyncio.wait_for(mark.stuck.wait(), timeout=10)

    await _close_the_inbox_channel_from_the_broker()

    await eventually(lambda: len(claims) == 2, timeout=10)  # 重投的那一份来领过了
    await asyncio.sleep(0.5)
    assert [m.message_id for m in world.got] == [delivery.message_id]
    row = await _inflight(test_db, delivery.message_id)
    assert row["state"] == "succeeded"
    assert row["last_error"] is None, "放开占位改了一条已经成功的记录"


async def test_a_delivery_cancelled_after_another_worker_took_its_claim_over_leaves_that_claim(
    broker, test_db, monkeypatch
):
    """租约过期、另一个进程已经接管了这条，这里的处理才被取消：放开只按这次占位的标记做，不碰
    接管者的那一行。"""
    monkeypatch.setattr(receiving, "STOP_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(
        receiving,
        "PROCESSING_RETRY",
        RetryPolicy(
            n=4, backoff="exponential", base_delay_ms=30_000, max_delay_ms=600_000, lease_ms=1_000
        ),
    )
    entered = asyncio.Event()

    async def never_finishes(message) -> None:
        entered.set()
        await asyncio.sleep(3600)

    inbox("world", on_message=never_finishes)
    await start_messaging()
    delivery = await send(sender="operator", recipient="world", body="被接管。")
    await asyncio.wait_for(entered.wait(), timeout=10)
    await asyncio.sleep(1.2)  # 租约过期
    taken = await claim_inflight(
        edge_id=f"inbox:world@{LANE}",
        idempotent_key=delivery.message_id,
        data_table="inbox_world",
        worker_id="another-process#1",
        lease_ms=600_000,
    )
    assert taken.action == "run"

    await asyncio.wait_for(stop_messaging(), timeout=10)

    row = await _inflight(test_db, delivery.message_id)
    assert (row["state"], row["worker_id"]) == ("processing", "another-process#1")
    assert row["last_error"] is None, "放开占位改了接管者的那一行"


async def test_a_question_cancelled_because_its_channel_closed_ends_as_cancelled(broker):
    """回答问题时它所在的通道被 broker 关掉：处理它的任务按取消结束。通道关着时不能再去确认它，
    读 ``incoming.channel`` 本身就会抛 ``ChannelInvalidStateError``，把取消换成另一个异常。"""
    answering: list[asyncio.Task] = []

    async def never_answers(question) -> str | None:
        answering.append(asyncio.current_task())
        await asyncio.sleep(3600)
        return None

    inbox("world", on_message=Inbox().on_message, on_question=never_answers)
    await start_messaging()
    asked = asyncio.create_task(
        ask(sender="operator", recipient="world", body="在吗？", timeout_seconds=30)
    )
    await eventually(lambda: answering, timeout=10)

    await _close_the_inbox_channel_from_the_broker(f"questions_world_{LANE}")

    (task,) = answering
    await asyncio.wait({task}, timeout=10)
    assert task.done()
    assert task.cancelled(), f"取消被换成了别的异常：{task.exception()!r}"
    asked.cancel()
