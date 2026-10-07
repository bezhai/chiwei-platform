"""通信机制测试共用的几样：一个会记账的收件箱处理函数、轮询等待、取记录里的状态列。"""
from __future__ import annotations

import asyncio
import inspect
import time
from datetime import UTC, datetime

from app.messaging.message import Message


class Inbox:
    """一个收件箱的处理函数：记下收到的每一条，能按需失败。"""

    def __init__(self, *, fail_times: int = 0, answer=None) -> None:
        self.got: list[Message] = []
        self.received_at: list[datetime] = []
        self.calls = 0
        self.questions: list[Message] = []
        self._fail_times = fail_times
        self._answer = answer

    async def on_message(self, message: Message) -> None:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError(f"处理失败（第 {self.calls} 次）")
        self.got.append(message)
        self.received_at.append(datetime.now(UTC))

    async def on_question(self, message: Message) -> str | None:
        self.questions.append(message)
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


async def eventually(predicate, *, timeout: float = 10.0, step: float = 0.05):
    """轮询到 ``predicate()`` 为真；它可以返回值，也可以返回一个要 await 的东西。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result and result != -1:
            return
        await asyncio.sleep(step)
    raise AssertionError("condition not met in time")


def outcomes(rows: list[dict]) -> list[str]:
    return [r["outcome"] for r in rows]


async def outcomes_become(message_id: str, expected: list[str], *, timeout: float = 10.0) -> list[str]:
    """等记录者里这条消息的各行写齐到 ``expected``，交回最后读到的那一串。

    "送达"那一行是在 broker 确认之后才写的，接收方可能先拿到消息：拿到消息那一刻去读记录，
    最后一行可能还没落下。等不到就交回最后读到的，让调用方的断言把差别打印出来。
    """
    from app.messaging.record import read_record

    deadline = time.monotonic() + timeout
    while True:
        got = outcomes(await read_record(message_id=message_id))
        if got == expected or time.monotonic() >= deadline:
            return got
        await asyncio.sleep(0.05)


class HangsOnce:
    """包住领取或标记的一步：第一次在 ``after_commit`` 指定的那一侧停住不返回，之后照常。"""

    def __init__(self, real, *, after_commit: bool) -> None:
        self._real = real
        self._after_commit = after_commit
        self.stuck = asyncio.Event()

    async def __call__(self, **kw):
        if self.stuck.is_set():
            return await self._real(**kw)
        if self._after_commit:
            result = await self._real(**kw)
            self.stuck.set()
            await asyncio.Event().wait()
            return result
        self.stuck.set()
        await asyncio.Event().wait()
        return await self._real(**kw)
