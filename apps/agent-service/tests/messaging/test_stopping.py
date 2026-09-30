"""进程停下时正在处理的消息：停止要等它们处理完；等不完的，放回去让下一个进程接着处理。

原来 ``stop_receiving`` 取消消费者、关掉通道就返回，正在处理的消息还在跑：它处理完要确认时
通道已经关了，确认失败、消息被 broker 重投；它的去重占位还是"处理中"，重投的那一份要等租约
过期才会有人接。进程关闭（部署、重启）时就是这个样子。跑在真 broker + 真 Postgres 上。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.infra.rabbitmq import ISOLATED_DEAD_LETTERS
from app.messaging.lifecycle import start_messaging, stop_messaging
from app.messaging.receiving import inbox
from app.messaging.sending import send

from .conftest import LANE
from .helpers import eventually

pytestmark = pytest.mark.usefixtures("messaging_db")


async def test_stopping_waits_for_a_message_being_handled(broker):
    entered = asyncio.Event()
    done: list[str] = []

    async def takes_a_moment(message) -> None:
        entered.set()
        await asyncio.sleep(0.8)
        done.append(message.message_id)

    inbox("world", on_message=takes_a_moment)
    await start_messaging()
    delivery = await send(sender="operator", recipient="world", body="正在处理。")
    await entered.wait()

    await stop_messaging()

    assert done == [delivery.message_id], "停止没等正在处理的消息"
    assert await broker.depth(f"inbox_world_{LANE}") == 0, "处理完了却没确认，会被重投"


async def test_a_message_still_running_after_the_grace_goes_back_and_is_taken_up_at_once(
    broker, monkeypatch, test_db
):
    """等不完的：取消、放回收件箱（不进死信），并且放开去重占位——下一个进程不用等租约过期。"""
    from app.messaging import receiving

    monkeypatch.setattr(receiving, "STOP_GRACE_SECONDS", 0.3)
    entered = asyncio.Event()

    async def never_finishes(message) -> None:
        entered.set()
        await asyncio.sleep(3600)

    # 处理时限 10 分钟 → 占位租约 11 分钟：不放开的话，重投的那一份要等 11 分钟。
    inbox("world", on_message=never_finishes, processing_timeout=timedelta(minutes=10))
    await start_messaging()
    delivery = await send(sender="operator", recipient="world", body="等不完。")
    await entered.wait()

    await asyncio.wait_for(stop_messaging(), timeout=10)

    assert await broker.depth(f"{ISOLATED_DEAD_LETTERS}_{LANE}") == 0, "被取消的消息进了死信"
    assert await broker.depth(f"inbox_world_{LANE}") == 1, "被取消的消息没有放回收件箱"
    async with test_db.begin() as conn:
        state = (
            await conn.execute(
                text("SELECT state FROM runtime_inflight WHERE idempotent_key = :k"),
                {"k": delivery.message_id},
            )
        ).scalar_one()
    assert state != "processing", "占位没放开，重投的那一份要等租约过期"

    from app.messaging.receiving import clear_inboxes

    clear_inboxes()
    handled: list[str] = []

    async def quick(message) -> None:
        handled.append(message.message_id)

    inbox("world", on_message=quick, processing_timeout=timedelta(minutes=10))
    await start_messaging()
    await eventually(lambda: handled, timeout=10)
    assert handled == [delivery.message_id]
