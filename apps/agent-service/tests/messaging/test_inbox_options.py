"""收件箱拥有者开设时可以声明的几件事：一条最多处理多久、一次只处理一条、开设时先做一件事、
一条消息最终处理失败时做一件事。

world 的主 agent 一轮可能跑得比默认的 15 分钟租约还久，一次只能想一件事，进程启动时
要按自己的私有状态决定要不要立刻醒一次，一轮最终失败时要自己安排再醒。这些都落在 broker
和去重租约上，所以跟 ``test_contract.py`` 一样跑在真 broker + 真 Postgres 上。
"""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest

from app.infra.rabbitmq import ISOLATED_DEAD_LETTERS, Route, mq
from app.messaging.broker import inbox_exists
from app.messaging.lifecycle import start_messaging
from app.messaging.message import Kind, Message
from app.messaging.receiving import inbox
from app.messaging.sending import send
from app.runtime.wire import RetryPolicy

from .conftest import LANE
from .helpers import eventually

pytestmark = pytest.mark.usefixtures("messaging_db")


def _fast_retry(monkeypatch, *, lease_ms: int = 60_000) -> None:
    from app.messaging import receiving

    monkeypatch.setattr(
        receiving,
        "PROCESSING_RETRY",
        RetryPolicy(n=3, backoff="linear", base_delay_ms=200, max_delay_ms=300, lease_ms=lease_ms),
    )


async def _publish_again(message_id: str, body: str) -> None:
    """同一条消息再投一份进 world 的收件箱，模拟至少一次投递下的重复。"""
    from datetime import UTC, datetime

    copy = Message(
        message_id=message_id,
        sender="operator",
        recipient="world",
        time=datetime.now(UTC),
        kind=Kind.MESSAGE,
        body=body,
    )
    assert await mq.publish_with_confirm(
        Route("inbox_world", "inbox.world", isolated=True), copy.to_json(), lane=LANE
    )


# ---------------------------------------------------------------------------
# 一条最多处理多久
# ---------------------------------------------------------------------------


async def test_processing_past_its_limit_is_a_failure_and_is_retried(broker, monkeypatch):
    """超过声明的时长还没处理完，算一次处理失败：被取消，然后按重试再来一次。"""
    _fast_retry(monkeypatch)
    entered: list[float] = []
    done: list[str] = []

    async def slow_then_quick(message) -> None:
        entered.append(time.monotonic())
        if len(entered) == 1:
            await asyncio.sleep(30)
        done.append(message.message_id)

    inbox(
        "world",
        on_message=slow_then_quick,
        processing_timeout=timedelta(milliseconds=500),
    )
    await start_messaging()

    delivery = await send(sender="operator", recipient="world", body="第一次会超时。")

    await eventually(lambda: done, timeout=10)
    assert done == [delivery.message_id]
    assert len(entered) == 2


async def test_the_claim_outlasts_the_processing_limit(broker, monkeypatch):
    """租约放长到处理时限之上：处理还没完时到的重复副本不会被当成"前一个进程死了"接管。

    默认租约调成 0.5 秒，处理要 3 秒：租约不跟着处理时限放长的话，重复那份在前一份还在
    处理时就能拿到占位，同一条消息被处理两遍。
    """
    from app.messaging import receiving

    _fast_retry(monkeypatch, lease_ms=500)
    # 时限 4 秒 + 0.5 秒 = 4.5 秒的租约：重复那份在 4.5 秒后被重新拿起时，前一份已经处理
    # 完了，它按"处理过"跳过——测试结束前就收拾干净，不会留一条迟到的副本给后面的用例。
    monkeypatch.setattr(receiving, "LEASE_OVER_TIMEOUT_MS", 500)
    running = 0
    overlaps: list[int] = []
    handled: list[str] = []

    async def takes_three_seconds(message) -> None:
        nonlocal running
        running += 1
        overlaps.append(running)
        try:
            await asyncio.sleep(3)
            handled.append(message.message_id)
        finally:
            running -= 1

    inbox(
        "world",
        on_message=takes_three_seconds,
        processing_timeout=timedelta(seconds=4),
    )
    await start_messaging()

    delivery = await send(sender="operator", recipient="world", body="只该处理一次。")
    await asyncio.sleep(0.2)
    await _publish_again(delivery.message_id, "只该处理一次。")

    await eventually(lambda: handled, timeout=10)
    # 等重复那份在租约过后被重新拿起、看到"处理过"而跳过。
    await asyncio.sleep(4)
    assert handled == [delivery.message_id]
    assert max(overlaps) == 1


# ---------------------------------------------------------------------------
# 一次只处理一条
# ---------------------------------------------------------------------------


async def _max_overlap(*, one_at_a_time: bool) -> int:
    running = 0
    peak = 0
    handled: list[str] = []

    async def handler(message) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        try:
            await asyncio.sleep(0.5)
            handled.append(message.message_id)
        finally:
            running -= 1

    inbox("world", on_message=handler, one_at_a_time=one_at_a_time)
    await start_messaging()
    for i in range(3):
        await send(sender="operator", recipient="world", body=f"第 {i} 条。")
    await eventually(lambda: len(handled) == 3, timeout=10)
    return peak


async def test_an_inbox_taking_one_at_a_time_never_handles_two_together(broker):
    assert await _max_overlap(one_at_a_time=True) == 1


async def test_an_ordinary_inbox_handles_several_together(broker):
    """对照：不声明的收件箱照旧并发处理，上一条用例的 1 不是巧合。"""
    assert await _max_overlap(one_at_a_time=False) > 1


# ---------------------------------------------------------------------------
# 开设时先做一件事
# ---------------------------------------------------------------------------


async def test_on_open_runs_once_after_the_inbox_exists_and_before_anything_is_handled(broker):
    """开设时的那一步看得到自己的收件箱，而且在它结束之前一条消息都不处理。"""
    order: list[str] = []

    async def on_open() -> None:
        order.append(f"open:{await inbox_exists('world')}")
        await send(sender="world", recipient="world", body="开设时发给自己的。")
        await asyncio.sleep(0.3)
        order.append("open done")

    async def on_message(message) -> None:
        order.append(f"message:{message.body}")

    inbox("world", on_message=on_message, on_open=on_open)
    await start_messaging()

    await eventually(lambda: len(order) == 3, timeout=10)
    assert order == ["open:True", "open done", "message:开设时发给自己的。"]


async def test_a_failing_on_open_fails_the_start(broker):
    """开设时那一步失败，启动就失败：不能带着一个没做完启动检查的收件箱看起来一切正常。"""

    async def on_open() -> None:
        raise RuntimeError("启动检查失败")

    async def on_message(message) -> None:  # pragma: no cover - never reached
        raise AssertionError

    inbox("world", on_message=on_message, on_open=on_open)
    with pytest.raises(RuntimeError, match="启动检查失败"):
        await start_messaging()


# ---------------------------------------------------------------------------
# 一条消息最终处理失败时
# ---------------------------------------------------------------------------


async def test_the_final_failure_hook_runs_once_after_the_last_attempt(broker, monkeypatch):
    """重试用完、即将进死信时调一次，带着那条消息和最后那次的异常；消息照常进死信。"""
    _fast_retry(monkeypatch)
    attempts: list[str] = []
    given_up: list[tuple[str, str, int]] = []

    async def always_fails(message) -> None:
        attempts.append(message.message_id)
        raise RuntimeError(f"第 {len(attempts)} 次失败")

    async def on_final_failure(message, error) -> None:
        given_up.append((message.message_id, str(error), len(attempts)))

    inbox("world", on_message=always_fails, on_final_failure=on_final_failure)
    await start_messaging()

    delivery = await send(sender="operator", recipient="world", body="一直失败。")

    dead_letters = f"{ISOLATED_DEAD_LETTERS}_{LANE}"
    await eventually(lambda: broker.depth(dead_letters), timeout=15)
    assert given_up == [(delivery.message_id, "第 3 次失败", 3)]
    assert await broker.depth(dead_letters) == 1


async def test_a_failing_final_failure_hook_still_dead_letters_the_message(broker, monkeypatch):
    _fast_retry(monkeypatch)

    async def always_fails(message) -> None:
        raise RuntimeError("处理失败")

    async def broken_hook(message, error) -> None:
        raise RuntimeError("钩子也失败了")

    inbox("world", on_message=always_fails, on_final_failure=broken_hook)
    await start_messaging()

    await send(sender="operator", recipient="world", body="x")

    dead_letters = f"{ISOLATED_DEAD_LETTERS}_{LANE}"
    await eventually(lambda: broker.depth(dead_letters), timeout=15)
    assert await broker.depth(dead_letters) == 1


async def test_the_hook_is_not_called_while_retries_remain(broker, monkeypatch):
    _fast_retry(monkeypatch)
    calls = {"n": 0}
    given_up: list[str] = []
    done: list[str] = []

    async def fails_twice(message) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("还有重试")
        done.append(message.message_id)

    async def on_final_failure(message, error) -> None:
        given_up.append(message.message_id)

    inbox("world", on_message=fails_twice, on_final_failure=on_final_failure)
    await start_messaging()

    await send(sender="operator", recipient="world", body="第三次成功。")

    await eventually(lambda: done, timeout=10)
    assert given_up == []
