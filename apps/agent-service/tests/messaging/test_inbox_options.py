"""收件箱拥有者开设时可以声明的几件事：一条最多处理多久、一次只处理一条、开设时先做一件事、
只在持有某样东西期间消费、点名哪条消息失败时不限次数重试。

world 的主 agent 一轮可能跑得比默认的 15 分钟租约还久，一次只能想一件事，进程启动时
要按自己的私有状态决定要不要立刻醒一次，只有拿着卷的写锁才消费，最新那次唤醒永不进死信。
这些都落在 broker 和去重租约上，所以跟 ``test_contract.py`` 一样跑在真 broker + 真 Postgres 上。
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from app.infra.rabbitmq import ISOLATED_DEAD_LETTERS, Route, mq
from app.messaging.broker import inbox_route, opened
from app.messaging.lifecycle import start_messaging, stop_messaging
from app.messaging.message import Kind, Message
from app.messaging.receiving import inbox, inboxes_at_start
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
        wakes_recipient=True,
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
        order.append(f"open:{await opened(inbox_route('world'))}")
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
# 只在持有某样东西期间消费
# ---------------------------------------------------------------------------


class ControlledHold:
    """一个由测试控制的 ``consume_while``：测试允许之前拿不到；拿到、放开都记一笔。"""

    def __init__(self, *, available_now: bool = False) -> None:
        self.available = asyncio.Event()
        if available_now:
            self.available.set()
        self.events: list[str] = []

    @asynccontextmanager
    async def hold(self):
        await self.available.wait()
        self.events.append("acquired")
        try:
            yield
        finally:
            self.events.append("released")


async def test_waiting_to_hold_neither_blocks_the_start_nor_consumes(broker):
    """拿不到就等，不消费；启动不被它卡住（进程照常起来、照常答健康检查）。开设时那一步也等拿到之后才跑。"""
    holder = ControlledHold()
    order: list[str] = []

    async def on_open() -> None:
        order.append("open")

    async def on_message(message) -> None:
        order.append(f"message:{message.body}")

    inbox("world", on_message=on_message, on_open=on_open, consume_while=holder.hold)
    await asyncio.wait_for(start_messaging(), timeout=5)

    await send(sender="operator", recipient="world", body="等着。")
    await asyncio.sleep(1.0)
    assert order == []
    assert await broker.depth(f"inbox_world_{LANE}") == 1

    holder.available.set()
    await eventually(lambda: len(order) == 2, timeout=10)
    assert order == ["open", "message:等着。"]


async def test_stopping_lets_go_only_after_the_message_being_handled(broker):
    holder = ControlledHold(available_now=True)
    entered = asyncio.Event()

    async def takes_a_moment(message) -> None:
        entered.set()
        await asyncio.sleep(0.8)
        holder.events.append("handled")

    inbox("world", on_message=takes_a_moment, consume_while=holder.hold)
    await start_messaging()
    await send(sender="operator", recipient="world", body="正在处理。")
    await entered.wait()

    await stop_messaging()

    assert holder.events == ["acquired", "handled", "released"]


async def test_stopping_while_still_waiting_to_hold_gives_up_the_wait(broker):
    holder = ControlledHold()

    async def on_message(message) -> None:  # pragma: no cover - never reached
        raise AssertionError

    inbox("world", on_message=on_message, consume_while=holder.hold)
    await start_messaging()

    await asyncio.wait_for(stop_messaging(), timeout=5)

    assert holder.events == []


async def test_a_failing_on_open_while_held_lets_go_and_tries_again(broker, monkeypatch):
    from app.messaging import receiving

    monkeypatch.setattr(receiving, "OPEN_RETRY_SECONDS", 0.2)
    holder = ControlledHold(available_now=True)
    attempts = {"n": 0}
    handled: list[str] = []

    async def on_open() -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("启动检查失败")

    async def on_message(message) -> None:
        handled.append(message.body)

    inbox("world", on_message=on_message, on_open=on_open, consume_while=holder.hold)
    await start_messaging()
    await send(sender="operator", recipient="world", body="第二次开设之后处理。")

    await eventually(lambda: handled, timeout=10)
    assert holder.events[:3] == ["acquired", "released", "acquired"]
    assert attempts["n"] == 2


# ---------------------------------------------------------------------------
# 拥有者点名的消息：失败时不限次数重试，永不进死信
# ---------------------------------------------------------------------------


def _capture_retry_delays(monkeypatch) -> list[int]:
    """记下每一次失败重投排的延时（毫秒），重投照常发出去。"""
    from app.messaging import receiving

    real = receiving.publish
    delays: list[int] = []

    async def publish(route, body, *, headers, delay_ms=None):
        if headers.get("x-delivery-count"):
            delays.append(delay_ms)
        return await real(route, body, headers=headers, delay_ms=delay_ms)

    monkeypatch.setattr(receiving, "publish", publish)
    return delays


async def test_a_singled_out_message_is_retried_past_the_limit_and_never_dead_lettered(
    broker, monkeypatch, caplog
):
    """重试次数上限是 3：它失败了 6 次，照样一直重试，第 7 次成功；每次失败记录者里有一行、日志里有 warning。"""
    import logging

    from app.messaging.record import read_record

    _fast_retry(monkeypatch)
    delays = _capture_retry_delays(monkeypatch)
    caplog.set_level(logging.WARNING, logger="app.messaging.receiving")
    calls = {"n": 0}
    done: list[str] = []

    async def fails_six_times(message) -> None:
        calls["n"] += 1
        if calls["n"] <= 6:
            raise RuntimeError(f"第 {calls['n']} 次失败")
        done.append(message.message_id)

    async def always(message):
        return timedelta(milliseconds=400)

    inbox("world", on_message=fails_six_times, retry_without_limit=always)
    await start_messaging()
    delivery = await send(sender="operator", recipient="world", body="会失败六次。")

    await eventually(lambda: done, timeout=20)
    assert calls["n"] == 7
    assert await broker.depth(f"{ISOLATED_DEAD_LETTERS}_{LANE}") == 0
    # 指数退避，封顶在拥有者给的上限：200、400、400……
    assert delays == [200, 400, 400, 400, 400, 400]
    rows = [r for r in await read_record(message_id=delivery.message_id) if r["outcome"] == "retrying"]
    assert len(rows) == 6
    assert "第 6 次失败" in rows[-1]["reason"]
    warnings = [r for r in caplog.records if "retrying without limit" in r.getMessage()]
    assert len(warnings) == 6


async def test_messages_the_owner_does_not_single_out_keep_the_limited_retries(
    broker, monkeypatch
):
    _fast_retry(monkeypatch)
    calls = {"n": 0}

    async def always_fails(message) -> None:
        calls["n"] += 1
        raise RuntimeError("失败")

    async def never(message):
        return None

    inbox("world", on_message=always_fails, retry_without_limit=never)
    await start_messaging()
    await send(sender="operator", recipient="world", body="普通消息。")

    dead_letters = f"{ISOLATED_DEAD_LETTERS}_{LANE}"
    await eventually(lambda: broker.depth(dead_letters), timeout=15)
    assert calls["n"] == 3


async def test_when_the_retry_copy_cannot_be_published_the_message_is_put_back_not_acked(
    broker, monkeypatch
):
    """延迟重投发不出去：原消息不能被确认丢掉，也不能进死信——放回队列，过一会儿再处理。"""
    from app.messaging import receiving
    from app.messaging.message import SendFailed

    _fast_retry(monkeypatch)
    monkeypatch.setattr(receiving, "PUT_BACK_BASE_SECONDS", 0.1)
    real = receiving.publish
    broken = {"left": 1}

    async def publish(route, body, *, headers, delay_ms=None):
        if headers.get("x-delivery-count") and broken["left"]:
            broken["left"] -= 1
            raise SendFailed("broker did not confirm the retry copy")
        return await real(route, body, headers=headers, delay_ms=delay_ms)

    monkeypatch.setattr(receiving, "publish", publish)
    calls = {"n": 0}
    done: list[str] = []

    async def fails_once(message) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("第一次失败")
        done.append(message.message_id)

    async def always(message):
        return timedelta(seconds=1)

    inbox("world", on_message=fails_once, retry_without_limit=always)
    await start_messaging()
    await send(sender="operator", recipient="world", body="重投发不出去。")

    await eventually(lambda: done, timeout=15)
    assert broken["left"] == 0, "故障没注入上"
    assert await broker.depth(f"{ISOLATED_DEAD_LETTERS}_{LANE}") == 0


# ---------------------------------------------------------------------------
# 名字到进程启动时才知道的收件箱
# ---------------------------------------------------------------------------


async def test_inboxes_named_at_start_are_opened_when_receiving_starts(broker):
    """名字存在库里的收件箱：接线时只声明"启动时再开"，开始接收时才去取名字、开设。"""
    got: list[Message] = []
    named: list[str] = []

    async def on_message(message) -> None:
        got.append(message)

    async def open_by_name() -> None:
        named.append("绫奈")  # 真实的拥有者在这里读库
        inbox("绫奈", on_message=on_message)

    inboxes_at_start(open_by_name)
    assert named == [], "声明时就去取名字了：接线 import 的时候库还没准备好"

    await start_messaging()
    delivery = await send(sender="world", recipient="绫奈", body="窗外下起了雨。")

    assert named == ["绫奈"]
    assert delivery.delivered
    await eventually(lambda: got)
    assert [m.message_id for m in got] == [delivery.message_id]


async def test_inboxes_named_at_start_are_named_once_per_process(broker):
    """停了再开始接收，开设的还是第一次取到的那几个名字，不再取一次。"""
    named: list[str] = []

    async def on_message(message) -> None:
        return None

    async def open_by_name() -> None:
        named.append("绫奈")
        inbox("绫奈", on_message=on_message)

    inboxes_at_start(open_by_name)
    await start_messaging()
    await stop_messaging()
    await start_messaging()

    assert named == ["绫奈"]
    assert await opened(inbox_route("绫奈"))


async def test_a_failure_while_naming_inboxes_at_start_fails_the_start(broker):
    """取名字那一步失败，启动就失败，而且一个收件箱都不开：名字就是地址，名字有问题时不能
    带着一部分收件箱看起来一切正常。"""

    async def on_message(message) -> None:
        return None

    async def names_are_wrong() -> None:
        raise RuntimeError("两个人的显示名一样")

    inbox("operator", on_message=on_message)
    inboxes_at_start(names_are_wrong)

    with pytest.raises(RuntimeError, match="两个人的显示名一样"):
        await start_messaging()
    assert not await opened(inbox_route("operator"))


async def test_a_failure_while_naming_inboxes_at_start_fails_every_later_start_too(broker):
    """同一个进程里再开始接收一次：取名字那一步照样再跑、照样失败，仍然一个收件箱都不开。
    第一次失败之前已经按名字开设了一半的那几个，也不能在第二次被当成"已经开好的"开出去。"""

    async def on_message(message) -> None:
        return None

    async def names_are_wrong() -> None:
        inbox("赤尾", on_message=on_message)  # 开了一个，下一个名字才发现有问题
        raise RuntimeError("两个人的显示名一样")

    inbox("operator", on_message=on_message)
    inboxes_at_start(names_are_wrong)

    with pytest.raises(RuntimeError, match="两个人的显示名一样"):
        await start_messaging()
    with pytest.raises(RuntimeError, match="两个人的显示名一样"):
        await start_messaging()

    assert not await opened(inbox_route("operator"))
    assert not await opened(inbox_route("赤尾"))


async def test_naming_inboxes_at_start_is_tried_again_in_full_after_a_failure(broker):
    """取名字那一步失败过（比如库一时连不上），再开始接收时整组重新取一遍：开出来的是完整的
    一组，不会因为上一次开了一半而撞上"已经声明过"。"""
    attempts: list[int] = []

    async def on_message(message) -> None:
        return None

    async def flaky() -> None:
        attempts.append(len(attempts) + 1)
        inbox("赤尾", on_message=on_message)
        if len(attempts) == 1:
            raise RuntimeError("库一时连不上")
        inbox("绫奈", on_message=on_message)

    inboxes_at_start(flaky)
    with pytest.raises(RuntimeError, match="库一时连不上"):
        await start_messaging()
    await start_messaging()

    assert attempts == [1, 2]
    assert await opened(inbox_route("赤尾"))
    assert await opened(inbox_route("绫奈"))


def test_clearing_the_inboxes_also_drops_the_ones_named_at_start():
    """测试之间靠 ``clear_inboxes`` 回到干净状态：留下一个启动时才取名字的声明，下一个用例
    开始接收时就会替上一个用例去读库。"""
    from app.messaging import receiving

    async def open_by_name() -> None:
        inbox("绫奈", on_message=None)  # 不会被调到

    inboxes_at_start(open_by_name)
    receiving.clear_inboxes()

    assert receiving.INBOXES_AT_START == []
