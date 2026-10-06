"""拥有者能问通信机制：这些消息 id 里，哪些在自己的收件箱已经有了定论——有一次投递处理成功、而且
记下来了，之后再来的同一条在交给处理函数之前就会被挡掉。

拥有者自己记着"处理过哪些"时，要知道什么时候可以不记了：处理函数返回不算，通信机制把成功记下来
才算。处理函数返回之后、记成功之前进程死了或者那一笔没写成，租约过期后同一条还会交到处理函数
手里。这里用真 broker、真 Postgres 看这条线落在哪。
"""
from __future__ import annotations

import uuid

import pytest

from app.messaging import receiving
from app.messaging.lifecycle import start_messaging
from app.messaging.receiving import inbox, succeeded_message_ids
from app.messaging.sending import send
from app.runtime.wire import RetryPolicy

from .helpers import eventually

pytestmark = pytest.mark.usefixtures("messaging_db")


async def test_only_ids_whose_success_is_recorded_are_finished(broker, monkeypatch):
    # 租约 0.5 秒：成功没记下来的那条，租约一过就重新交给处理函数，测试结束前收拾干净。
    monkeypatch.setattr(
        receiving,
        "PROCESSING_RETRY",
        RetryPolicy(n=3, backoff="linear", base_delay_ms=200, max_delay_ms=300, lease_ms=500),
    )
    done, broken, unrecorded = (uuid.uuid4().hex for _ in range(3))
    returned: list[str] = []
    attempts: list[str] = []

    async def handler(message) -> None:
        attempts.append(message.message_id)
        if message.message_id == broken:
            raise RuntimeError("处理不了")
        returned.append(message.message_id)

    real_mark = receiving.mark_succeeded
    failed_once: list[str] = []

    async def mark_succeeded(**kw):
        if kw["idempotent_key"] == unrecorded and not failed_once:
            failed_once.append(unrecorded)
            raise RuntimeError("记成功的那一笔没写成")
        return await real_mark(**kw)

    monkeypatch.setattr(receiving, "mark_succeeded", mark_succeeded)
    inbox("world", on_message=handler)
    await start_messaging()
    for message_id in (done, broken, unrecorded):
        await send(sender="operator", recipient="world", body="x", message_id=message_id)

    await eventually(lambda: done in returned and unrecorded in returned, timeout=10)
    await eventually(lambda: attempts.count(broken) == 3, timeout=10)  # 重试用完，进了死信
    unknown = uuid.uuid4().hex

    # 处理函数返回了、成功却没记下来的那条不算；处理失败的、没见过的也不算。
    assert await succeeded_message_ids("world", [done, broken, unrecorded, unknown]) == {done}
    # 别的收件箱的定论不算这个收件箱的。
    assert await succeeded_message_ids("operator", [done]) == set()

    # 租约过期，同一条又交到处理函数手里，这一次成功记下来了：它也有了定论。
    await eventually(lambda: returned.count(unrecorded) == 2, timeout=10)

    async def recorded():
        return unrecorded in await succeeded_message_ids("world", [unrecorded])

    await eventually(recorded, timeout=10)


async def test_asking_about_no_ids_reads_nothing(monkeypatch):
    async def no_database(**_kw):  # pragma: no cover - must not be reached
        raise AssertionError("没有要问的 id，不该读库")

    monkeypatch.setattr(receiving, "succeeded_keys", no_database)
    assert await succeeded_message_ids("world", []) == set()
