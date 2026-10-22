"""终止连接被宽限期截断时，只收回宽限期自己那一次取消，别人的取消照样生效（T9 决定 6）。

:meth:`app.data.dialect.AsyncpgDialect.do_terminate` 在宽限期到了时再取消一次任务，让 SQLAlchemy
直接断开连接，然后把这一次取消收回去。收回之前，同一个任务可能已经被别人要求取消了（比如外层的
期限也到了），而任务还没恢复运行：两次请求只交给它一个 CancelledError。这时不能把它当成自己那一次
吞掉，不然外层的取消就丢了，任务接着往下跑，再也没有第二次取消来叫停它。

这里用 SQLAlchemy 真的终止流程（``AsyncAdapt_terminate.terminate``，在 ``greenlet_spawn`` 里跑），
只把优雅关闭换成一个永远等不到的等待。两个取消落在同一轮事件循环里：先用一个阻塞回调把事件循环卡住，
等两个定时都过了点，它们才在同一轮里一前一后执行，任务在下一轮才恢复。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy.connectors.asyncio import AsyncAdapt_terminate
from sqlalchemy.util import await_only, greenlet_spawn

from app.data import dialect
from app.data.dialect import AsyncpgDialect

GRACE = 0.06


class CloseNeverAnswered(AsyncAdapt_terminate):
    """优雅关闭一直等着；被取消时 SQLAlchemy 改为直接断开，记下这一下。"""

    await_ = staticmethod(await_only)

    def __init__(self) -> None:
        self.dropped = False

    async def _terminate_graceful_close(self) -> None:
        await asyncio.Event().wait()

    def _terminate_force_close(self) -> None:
        self.dropped = True


@pytest.fixture(autouse=True)
def short_grace(monkeypatch):
    monkeypatch.setattr(dialect, "TERMINATE_GRACE_SECONDS", GRACE)


async def _terminate(*, already_cancelling: bool, other_cancel_at: float | None):
    """在一个任务里终止一条关不掉的连接。``other_cancel_at`` 是另一个取消请求离进入终止有多久
    （秒）；这两个定时都在事件循环被卡住的那段时间里过点，同一轮里执行。交回 (终止是不是抛了取消,
    终止之后任务身上还挂着几次取消请求, 连接是不是被直接断开了)。"""
    connection = CloseNeverAnswered()

    async def run():
        task = asyncio.current_task()
        if already_cancelling:
            # 一次已经交付、正在往外传的取消（比如外层期限到了，SQLAlchemy 正在作废连接）。
            task.cancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                pass
        loop = asyncio.get_running_loop()
        loop.call_later(0.01, time.sleep, 0.3)
        if other_cancel_at is not None:
            loop.call_later(other_cancel_at, task.cancel)
        try:
            await greenlet_spawn(AsyncpgDialect().do_terminate, connection)
        except asyncio.CancelledError:
            return True, task.cancelling(), connection.dropped
        return False, task.cancelling(), connection.dropped

    return await asyncio.create_task(run())


@pytest.mark.parametrize("already_cancelling", [False, True])
async def test_with_no_one_else_cancelling_the_grace_drops_the_connection_and_leaves_no_trace(
    already_cancelling,
):
    raised, pending, dropped = await _terminate(
        already_cancelling=already_cancelling, other_cancel_at=None
    )

    assert dropped
    assert not raised
    assert pending == (1 if already_cancelling else 0)


@pytest.mark.parametrize("already_cancelling", [False, True])
@pytest.mark.parametrize(
    "other_cancel_at", [GRACE - 0.02, GRACE + 0.02], ids=["before the grace", "after the grace"]
)
async def test_a_cancellation_asked_for_while_the_grace_runs_out_still_stands(
    already_cancelling, other_cancel_at
):
    raised, pending, dropped = await _terminate(
        already_cancelling=already_cancelling, other_cancel_at=other_cancel_at
    )

    assert dropped
    assert raised, "the other cancellation was swallowed with the grace's own"
    assert pending == (2 if already_cancelling else 1)
