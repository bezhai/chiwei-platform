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
