"""world 每轮开头问通信机制"哪些处理完的消息已经记下了成功"，这一步有时限
（:data:`app.world.rounds.SETTLED_QUERY_TIMEOUT`，T9 决定 3）：超过就按这次没查到处理，这一轮照常跑。

这一步的查询被时限取消之后还要收尾。数据库断开的消息传不回进程时（:mod:`tests.data.db_proxy`），
收尾要等一件永远等不到的事；它最多再等 :data:`app.data.dialect.TERMINATE_GRACE_SECONDS`（决定 6），
这个时限才真的管用，不然这一轮一直卡到投递的处理时限。查询本身被一把表锁挡在数据库里。跑在真
Postgres 上，时限都缩短到一秒以内。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.data import dialect
from app.messaging import receiving
from app.messaging.message import Kind, new_message
from app.messaging.receiving import succeeded_message_ids as asks_messaging
from tests.data.db_proxy import process_db_behind
from tests.messaging.conftest import messaging_db  # noqa: F401
from tests.runtime.conftest import test_db, test_db_dsn  # noqa: F401

QUERY_TIMEOUT = 0.3
GRACE = 0.5
# 测试机上调度、收尾本身要花的时间。
SLACK = 1.5


def _from(sender: str, body: str):
    return new_message(sender=sender, recipient="world", body=body, kind=Kind.MESSAGE)


@pytest.fixture
async def process_db(messaging_db, test_db_dsn, monkeypatch):  # noqa: F811
    monkeypatch.setattr(dialect, "TERMINATE_GRACE_SECONDS", GRACE)
    async with process_db_behind(test_db_dsn, monkeypatch) as (_proxy, engine):
        yield engine


async def test_a_round_start_query_held_up_in_the_database_holds_the_round_only_for_its_time_limit(
    world, process_db, test_db, monkeypatch  # noqa: F811
):
    monkeypatch.setattr(
        "app.world.rounds.SETTLED_QUERY_TIMEOUT", timedelta(seconds=QUERY_TIMEOUT)
    )
    await world.deliver(_from("赤尾", "我出门了。"))  # 留下一条处理完的记录，下一轮开头要问它
    monkeypatch.setattr(receiving, "succeeded_message_ids", asks_messaging)

    async with test_db.begin() as other:
        await other.execute(text("LOCK TABLE runtime_inflight IN ACCESS EXCLUSIVE MODE"))
        delivery = asyncio.create_task(world.deliver(_from("千凪", "我在做饭。")))
        await asyncio.wait({delivery}, timeout=QUERY_TIMEOUT + GRACE + SLACK)
        held = not delivery.done()
        if held:
            delivery.cancel()
            await asyncio.wait({delivery}, timeout=5)

    assert not held, "the round waited on the cleanup of its cancelled start query"
    assert delivery.exception() is None
    assert len(world.runner.runs) == 2
    assert process_db.pool.checkedout() == 0
