"""代理按协议消息认出进程发出的 COMMIT，不按一次读到的字节（:mod:`tests.data.db_proxy`）。

TCP 不保证一次读到的正好是一条消息：一条消息可能分两次读到，也可能和前后的消息一次读到。
这里不用 Postgres：代理后面接一个只记下收到了什么的假数据库，进程这一头直接写协议字节。
"""
from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator

import pytest

from tests.data.db_proxy import CloseNeverReturns


def _untyped(code: int, body: bytes = b"") -> bytes:
    """启动阶段的消息：没有类型字节，长度（含自身）之后是一个代码。"""
    return struct.pack("!ii", 8 + len(body), code) + body


def _query(sql: str) -> bytes:
    """简单查询协议的 'Q' 消息。"""
    body = sql.encode() + b"\x00"
    return b"Q" + struct.pack("!i", 4 + len(body)) + body


# asyncpg 连上来先发 SSLRequest，数据库不接受加密时接着发 StartupMessage（协议 3.0）。
HANDSHAKE = _untyped(80877103) + _untyped(196608, b"user\x00test\x00\x00")
BEFORE = _query("SELECT 1;")
COMMIT = _query("COMMIT;")
AFTER = _query("SELECT 2;")


class _Database:
    """代理后面的假数据库：记下收到的字节，什么也不回。"""

    def __init__(self) -> None:
        self.received = bytearray()
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def _serve(self, reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            self.received += data

    async def has_received(self, expected: bytes) -> None:
        """等到收够 ``expected`` 那么多字节，收到的必须正好是它。"""
        async with asyncio.timeout(5):
            while len(self.received) < len(expected):
                await asyncio.sleep(0.01)
        assert bytes(self.received) == expected

    def close(self) -> None:
        if self._server is not None:
            self._server.close()


@pytest.fixture
async def connection() -> AsyncIterator[tuple[CloseNeverReturns, asyncio.StreamWriter, _Database]]:
    """一条走代理、已经过了启动阶段的连接：交回代理、进程这一头、假数据库。"""
    database = _Database()
    proxy = CloseNeverReturns("127.0.0.1", await database.start())
    _reader, process = await asyncio.open_connection("127.0.0.1", await proxy.start())
    try:
        process.write(HANDSHAKE)
        await process.drain()
        await database.has_received(HANDSHAKE)
        yield proxy, process, database
    finally:
        proxy.close()
        process.close()
        database.close()


async def _caught(held) -> None:
    async with asyncio.timeout(5):
        await held.caught.wait()


async def test_a_commit_that_arrives_in_two_reads_is_held(connection):
    proxy, process, database = connection
    held = proxy.hold_next_commit()

    process.write(COMMIT[:3])
    await process.drain()
    await asyncio.sleep(0.05)  # 让代理先把前半条读走
    process.write(COMMIT[3:])
    await process.drain()

    await _caught(held)
    await database.has_received(HANDSHAKE)
    held.deliver()
    await database.has_received(HANDSHAKE + COMMIT)


async def test_a_commit_read_together_with_other_messages_is_held_in_order(connection):
    proxy, process, database = connection
    held = proxy.hold_next_commit()

    process.write(BEFORE + COMMIT + AFTER)
    await process.drain()

    await _caught(held)
    await database.has_received(HANDSHAKE + BEFORE)
    held.deliver()
    await database.has_received(HANDSHAKE + BEFORE + COMMIT + AFTER)
