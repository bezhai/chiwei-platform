"""测试里让进程的数据库连接走一个转发代理：数据库关掉连接时，进程这一头收不到。

coe 那条网络路径上就是这样（2026-10-06）：数据库关连接的消息传不回进程。asyncpg 关连接时、通知
数据库停下一条查询之后，都要等数据库那头把连接关掉，在这条路径上就一直等着。代理复现的就是这一点；
:meth:`CloseNeverReturns.stall` 另外让数据库发回来的东西停住，像网络卡住了。

连接池按进程里真正用的那一套建（:func:`app.data.session.make_engine`）。要缩短的时限，在进入
:func:`process_db_behind` 之前用 monkeypatch 改好，建连接池时读的是改过的值。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.data import session as session_mod


class CloseNeverReturns:
    """转发代理：两头的字节照常转发；数据库关掉它那一头时，进程这一头不关。"""

    def __init__(self, host: str, port: int) -> None:
        self._host, self._port = host, port
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._flowing = asyncio.Event()
        self._flowing.set()
        # 数据库已经关掉、进程却没收到的连接数。
        self.unanswered_closes = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    def stall(self) -> None:
        """从现在起，数据库发回来的东西停在代理这里，不再转给进程。"""
        self._flowing.clear()

    async def _serve(self, from_process, to_process) -> None:
        from_db, to_db = await asyncio.open_connection(self._host, self._port)
        self._writers += [to_process, to_db]

        async def pump(reader, writer, *, gate: asyncio.Event | None = None) -> bool:
            """转发到对面关掉为止；交回是不是对面正常关掉的。"""
            try:
                while data := await reader.read(65536):
                    if gate is not None:
                        await gate.wait()
                    writer.write(data)
                    await writer.drain()
            except (ConnectionError, OSError):
                return False
            return True

        async def process_to_db() -> None:
            if await pump(from_process, to_db):
                to_db.close()

        async def db_to_process() -> None:
            if await pump(from_db, to_process, gate=self._flowing):
                self.unanswered_closes += 1

        await asyncio.gather(process_to_db(), db_to_process())

    def close(self) -> None:
        self._flowing.set()
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()


@asynccontextmanager
async def process_db_behind(
    dsn: str, monkeypatch
) -> AsyncIterator[tuple[CloseNeverReturns, AsyncEngine]]:
    """进程的数据库连接改走 :class:`CloseNeverReturns`，交回代理和连接池。"""
    url = make_url(dsn)
    proxy = CloseNeverReturns(url.host, url.port)
    port = await proxy.start()
    engine = session_mod.make_engine(url.set(host="127.0.0.1", port=port))
    monkeypatch.setattr(session_mod, "engine", engine)
    monkeypatch.setattr(
        session_mod,
        "async_session",
        async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False),
    )
    try:
        yield proxy, engine
    finally:
        # 先关代理：挂着的那些这时才等到对面关掉，释放连接池不会再卡住。
        proxy.close()
        await engine.dispose()
