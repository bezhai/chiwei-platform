"""数据库那边断开了、断开的消息却传不回进程时，投递不能一直挂着（2026-10-07）。

10-06 在 coe-world 上，20 条 world 的自定醒来同时到点。同时用到的连接超过连接池的 10 个，多出来的
是临时连接，用完要关掉。asyncpg 关连接时告诉数据库要断开，然后等数据库那边把连接关掉；那条网络
路径上数据库关连接的消息传不回进程，这一步就一直等着。它落在领取那一步的会话退出里，在处理时限
之外，也在"被取消就放开占位"之外，9 条投递一直挂到占位租约过期。

这里用一个转发代理复现那条网络路径：进程发过去的照常转给数据库，数据库关掉它那一头时，进程这一头
保持开着。连接池按进程里真正用的那一套建（:func:`app.data.session.make_engine`），只是语句时限
（:data:`app.data.session.COMMAND_TIMEOUT_SECONDS`）换成几秒，测试不用等一分钟。跑在真 broker +
真 Postgres 上。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.data import session as session_mod
from app.infra.cst_time import now_cst
from app.messaging import receiving
from app.messaging.lifecycle import start_messaging
from app.messaging.receiving import inbox
from app.messaging.sending import send_at

from .helpers import Inbox, eventually

pytestmark = pytest.mark.usefixtures("messaging_db")

# 同时到点的定时消息条数：定时队列和收件箱各拿 10 条（prefetch），同时要用的连接超过池里的 10 个。
BURST = 20

# 测试里的语句时限（秒）。
SHORT_COMMAND_TIMEOUT = 2


class CloseNeverReturns:
    """转发代理：两头的字节照常转发；数据库关掉它那一头时，进程这一头不关。"""

    def __init__(self, host: str, port: int) -> None:
        self._host, self._port = host, port
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        # 数据库已经关掉、进程却没收到的连接数。
        self.unanswered_closes = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def _serve(self, from_process, to_process) -> None:
        from_db, to_db = await asyncio.open_connection(self._host, self._port)
        self._writers += [to_process, to_db]

        async def pump(reader, writer) -> bool:
            """转发到对面关掉为止；交回是不是对面正常关掉的。"""
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            except (ConnectionError, OSError):
                return False
            return True

        async def process_to_db() -> None:
            if await pump(from_process, to_db):
                to_db.close()

        async def db_to_process() -> None:
            if await pump(from_db, to_process):
                self.unanswered_closes += 1

        await asyncio.gather(process_to_db(), db_to_process())

    def close(self) -> None:
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()


@pytest.fixture
async def db_behind_the_proxy(test_db, test_db_dsn, monkeypatch):
    """进程的数据库连接改走 :class:`CloseNeverReturns`，按 :func:`make_engine` 建。"""
    url = make_url(test_db_dsn)
    proxy = CloseNeverReturns(url.host, url.port)
    port = await proxy.start()
    monkeypatch.setattr(session_mod, "COMMAND_TIMEOUT_SECONDS", SHORT_COMMAND_TIMEOUT)
    engine = session_mod.make_engine(url.set(host="127.0.0.1", port=port))
    monkeypatch.setattr(session_mod, "engine", engine)
    monkeypatch.setattr(
        session_mod,
        "async_session",
        async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False),
    )
    yield proxy
    # 先关代理：挂着的关连接这时才等到对面关掉，释放连接池不会再卡住。
    proxy.close()
    await engine.dispose()


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
