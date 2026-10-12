"""测试里让进程的数据库连接走一个转发代理：数据库关掉连接时，进程这一头收不到。

coe 那条网络路径上就是这样（2026-10-06）：数据库关连接的消息传不回进程。asyncpg 关连接时、通知
数据库停下一条查询之后，都要等数据库那头把连接关掉，在这条路径上就一直等着。代理复现的就是这一点；
:meth:`CloseNeverReturns.stall` 另外让数据库发回来的东西停住，像网络卡住了。
:meth:`CloseNeverReturns.hold_next_commit` 把进程发出的下一个 COMMIT 扣在代理这里（:class:`HeldCommit`）：
进程发出了 COMMIT、等不到回复，数据库什么时候收到它、收不收得到由测试决定。

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

# asyncpg 提交事务发的就是这一条：简单查询协议的 'Q' 消息（类型、长度、以 0 结尾的语句）。
_COMMIT_STATEMENT = b"COMMIT;\x00"
_COMMIT = b"Q" + (4 + len(_COMMIT_STATEMENT)).to_bytes(4, "big") + _COMMIT_STATEMENT


class HeldCommit:
    """进程发出、被代理扣下的那个 COMMIT（:meth:`CloseNeverReturns.hold_next_commit`）。

    扣着的时候，进程已经发出 COMMIT、在等回复；数据库还没收到，事务连同它拿着的锁都还开着。
    这条连接上数据库再发回来的任何东西都到不了进程：COMMIT 后来怎样，进程都不会知道。
    """

    def __init__(self) -> None:
        self.caught = asyncio.Event()
        self._passed_on: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._to_process: asyncio.StreamWriter | None = None

    def cut_process_side(self) -> None:
        """关掉进程那一头，像网络把连接断了：进程等回复时看到连接没了。"""
        assert self._to_process is not None, "no COMMIT is held yet"
        self._to_process.close()

    def deliver(self) -> None:
        """把 COMMIT 转给数据库：它现在提交。"""
        self._passed_on.set_result(True)

    def lose(self) -> None:
        """丢掉 COMMIT、关掉数据库那一头：数据库看到连接在事务中途断了，回滚。"""
        self._passed_on.set_result(False)

    async def _catch(self, to_process: asyncio.StreamWriter) -> bool:
        """扣下，等测试决定；交回要不要转给数据库。"""
        self._to_process = to_process
        self.caught.set()
        return await self._passed_on


class CloseNeverReturns:
    """转发代理：两头的字节照常转发；数据库关掉它那一头时，进程这一头不关。"""

    def __init__(self, host: str, port: int) -> None:
        self._host, self._port = host, port
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._flowing = asyncio.Event()
        self._flowing.set()
        self._holds: list[HeldCommit] = []
        # 数据库已经关掉、进程却没收到的连接数。
        self.unanswered_closes = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    def stall(self) -> None:
        """从现在起，数据库发回来的东西停在代理这里，不再转给进程。"""
        self._flowing.clear()

    def hold_next_commit(self) -> HeldCommit:
        """从现在起，任何一条连接上进程发出的下一个 COMMIT 扣在代理这里，见 :class:`HeldCommit`。"""
        held = HeldCommit()
        self._holds.append(held)
        return held

    def _to_hold(self, data: bytes) -> HeldCommit | None:
        """``data`` 是一个 COMMIT、而且有一个还没扣到的 :class:`HeldCommit` 在等它时，交回那一个。"""
        if data != _COMMIT:
            return None
        return next((held for held in self._holds if not held.caught.is_set()), None)

    async def _serve(self, from_process, to_process) -> None:
        from_db, to_db = await asyncio.open_connection(self._host, self._port)
        self._writers += [to_process, to_db]
        held: HeldCommit | None = None

        async def process_to_db() -> None:
            """转发到进程那头关掉为止；正常关掉的，数据库这头也关。"""
            nonlocal held
            try:
                while data := await from_process.read(65536):
                    if held is None:
                        held = self._to_hold(data)
                        if held is not None and not await held._catch(to_process):
                            to_db.close()
                            return
                    to_db.write(data)
                    await to_db.drain()
            except (ConnectionError, OSError):
                return
            to_db.close()

        async def db_to_process() -> None:
            """转发到数据库那头关掉为止；正常关掉的，记一次没转达的关闭，进程这头不关。"""
            try:
                while data := await from_db.read(65536):
                    await self._flowing.wait()
                    if held is not None:
                        continue  # 扣下的 COMMIT 的回复，以及之后的一切
                    to_process.write(data)
                    await to_process.drain()
            except (ConnectionError, OSError):
                return
            self.unanswered_closes += 1

        await asyncio.gather(process_to_db(), db_to_process())

    def close(self) -> None:
        self._flowing.set()
        for held in self._holds:
            if not held._passed_on.done():
                held.lose()
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
