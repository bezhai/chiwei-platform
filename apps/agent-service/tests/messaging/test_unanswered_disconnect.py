"""数据库那边断开了、断开的消息却传不回进程时，投递不能一直挂着（2026-10-07）。

10-06 在 coe-world 上，20 条 world 的自定醒来同时到点。同时用到的连接超过连接池的 10 个，多出来的
是临时连接，用完要关掉。asyncpg 关连接时告诉数据库要断开，然后等数据库那边把连接关掉；那条网络
路径上数据库关连接的消息传不回进程，这一步就一直等着。它落在领取那一步的会话退出里，在处理时限
之外，也在"被取消就放开占位"之外，9 条投递一直挂到占位租约过期。

这里用一个转发代理复现那条网络路径（:mod:`tests.data.db_proxy`）：进程发过去的照常转给数据库，
数据库关掉它那一头时，进程这一头保持开着。连接池按进程里真正用的那一套建，只是语句时限
（:data:`app.data.session.COMMAND_TIMEOUT_SECONDS`）换成几秒，测试不用等一分钟。跑在真 broker +
真 Postgres 上。
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from app.data import session as session_mod
from app.infra.cst_time import now_cst
from app.messaging import receiving
from app.messaging.lifecycle import start_messaging
from app.messaging.receiving import inbox
from app.messaging.sending import send_at
from tests.data.db_proxy import process_db_behind

from .helpers import Inbox, eventually

pytestmark = pytest.mark.usefixtures("messaging_db")

# 同时到点的定时消息条数：定时队列和收件箱各拿 10 条（prefetch），同时要用的连接超过池里的 10 个。
BURST = 20

# 测试里的语句时限（秒）。
SHORT_COMMAND_TIMEOUT = 2


@pytest.fixture
async def db_behind_the_proxy(test_db, test_db_dsn, monkeypatch):
    """进程的数据库连接改走 :class:`tests.data.db_proxy.CloseNeverReturns`。"""
    monkeypatch.setattr(session_mod, "COMMAND_TIMEOUT_SECONDS", SHORT_COMMAND_TIMEOUT)
    async with process_db_behind(test_db_dsn, monkeypatch) as (proxy, _engine):
        yield proxy


async def test_a_burst_larger_than_the_pool_finishes_when_closing_a_connection_gets_no_answer(
    db_behind_the_proxy, broker, test_db, monkeypatch
):
    # 修之前挂着的投递要在测试收尾时停下来，不用等 20 秒。
    monkeypatch.setattr(receiving, "STOP_GRACE_SECONDS", 1.0)
    world = Inbox()
    inbox("world", on_message=world.on_message)
    due = now_cst() + timedelta(seconds=3)
    sent = [
        await send_at(sender="world", recipient="world", body=f"第 {i} 次醒来。", at=due)
        for i in range(BURST)
    ]
    await start_messaging()

    async def unfinished() -> dict[str, str]:
        async with test_db.connect() as conn:
            rows = await conn.execute(
                text("SELECT edge_id, idempotent_key, state FROM runtime_inflight")
            )
            states = {f"{edge} {key}": state for edge, key, state in rows.all()}
        # 每条消息两处占位：定时队列转交一次，收件箱处理一次。
        missing = 2 * BURST - len(states)
        stuck = {k: s for k, s in states.items() if s != "succeeded"}
        return {**stuck, **({"(not claimed yet)": str(missing)} if missing else {})}

    async def all_finished() -> bool:
        return not await unfinished()

    try:
        await eventually(all_finished, timeout=30, step=0.2)
    except AssertionError:
        pytest.fail(f"deliveries still unfinished after 30s: {await unfinished()}")

    assert sorted(m.message_id for m in world.got) == sorted(sent)
    assert db_behind_the_proxy.unanswered_closes > 0, (
        "没有一次关连接落在数据库关掉、进程收不到的情形上，这个测试没测到它要测的"
    )
