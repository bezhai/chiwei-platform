"""问题走收件箱旁边自己的那条队列，不排在普通消息后面。

world 一轮可能跑好几分钟：一次投递最多要等两轮（一轮的时限 10 分钟）、只在拿着卷的写锁时消费。问题要是
跟普通消息共用一条队列，一轮正在跑的时候来的问题就排在它后面，提问方等到截止时刻只能拿到
"没有回答"。这里钉住：

* 一轮正在跑、后面还排着消息，问题照样在截止时刻之前答上；
* 没拿到 ``consume_while`` 的进程（它不处理普通消息，``on_open`` 也还没跑）照样答问题；
* 拥有者还跑着没有问题队列的旧代码时，提问方立刻拿到"没有送达"，不白等到截止时刻。

跑在真 broker + 真 Postgres 上。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.infra.rabbitmq import Route, mq
from app.messaging import receiving
from app.messaging.lifecycle import start_messaging
from app.messaging.receiving import inbox
from app.messaging.record import read_record
from app.messaging.sending import NO_INBOX, ask, send

from .conftest import LANE
from .helpers import eventually, outcomes

pytestmark = pytest.mark.usefixtures("messaging_db")


class Hold:
    """一个由测试控制的 ``consume_while``：测试允许之前拿不到。"""

    def __init__(self, *, available_now: bool) -> None:
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


async def answer_the_kitchen(question) -> str:
    return "厨房里灯亮着，水壶在响。"


async def test_a_question_is_answered_while_a_long_round_holds_the_inbox(broker):
    """跟 world 一样开设：处理时限一个小时、只在持有期间消费。一轮卡住、另一条消息也在等着时
    问它，在提问方的截止时刻之前答上；那一轮和等着的那条都不受影响。"""
    holder = Hold(available_now=True)
    entered = asyncio.Event()
    release = asyncio.Event()
    handled: list[str] = []

    async def long_round(message) -> None:
        entered.set()
        await release.wait()
        handled.append(message.body)

    inbox(
        "world",
        on_message=long_round,
        on_question=answer_the_kitchen,
        processing_timeout=timedelta(minutes=21),
        consume_while=holder.hold,
    )
    await start_messaging()
    await send(sender="operator", recipient="world", body="第一轮。")
    await asyncio.wait_for(entered.wait(), timeout=10)
    await send(sender="operator", recipient="world", body="后到的一条。")

    started = time.monotonic()
    answer = await ask(
        sender="operator", recipient="world", body="厨房现在什么样？", timeout_seconds=5
    )

    assert answer.answered and answer.text == "厨房里灯亮着，水壶在响。"
    assert time.monotonic() - started < 5
    assert handled == [], "那一轮还没跑完"

    release.set()
    await eventually(lambda: len(handled) == 2, timeout=10)
    assert sorted(handled) == sorted(["第一轮。", "后到的一条。"])


async def test_a_process_not_holding_consume_while_still_answers_questions(broker):
    """问题不等 ``consume_while``，也不等 ``on_open``：没拿到锁的进程不处理普通消息，问题照答。"""
    holder = Hold(available_now=False)
    opened: list[bool] = []

    async def on_open() -> None:
        opened.append(True)

    async def on_message(message) -> None:  # pragma: no cover - never reached
        raise AssertionError("没持有的进程不该处理普通消息")

    inbox(
        "world",
        on_message=on_message,
        on_question=answer_the_kitchen,
        on_open=on_open,
        consume_while=holder.hold,
    )
    await start_messaging()
    await send(sender="operator", recipient="world", body="等拿到锁再处理。")

    answer = await ask(
        sender="operator", recipient="world", body="厨房现在什么样？", timeout_seconds=5
    )

    assert answer.answered
    assert holder.events == [] and opened == []
    assert await broker.depth(f"inbox_world_{LANE}") == 1


async def test_asking_an_owner_whose_inbox_has_no_question_queue_is_not_delivered(broker):
    """拥有者还跑着旧代码：收件箱在，问题队列不在。提问立刻拿到"没有送达"并照样记录，不投进
    收件箱（旧代码会把它当普通消息处理），也不白等到截止时刻。"""
    await start_messaging()
    await mq.declare_route(Route("inbox_world", "inbox.world", isolated=True), lane=LANE)

    started = time.monotonic()
    answer = await ask(sender="operator", recipient="world", body="在吗？", timeout_seconds=30)

    assert not answer.answered and answer.reason == NO_INBOX
    assert time.monotonic() - started < 5
    assert outcomes(await read_record(message_id=answer.question_id)) == ["not_delivered"]
    assert await broker.depth(f"inbox_world_{LANE}") == 0


class NeverAnswers:
    """回答函数：一直想下去，记下被叫了几次、是不是被打断了。"""

    def __init__(self) -> None:
        self.calls = 0
        self.cut_off = 0

    async def on_question(self, question) -> str | None:
        self.calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cut_off += 1
            raise
        return None


def _nothing_being_handled() -> bool:
    return not [t for t in receiving._in_flight if not t.done()]


async def test_an_answer_still_thinking_at_the_delivery_deadline_ends_as_no_answer(
    broker, monkeypatch
):
    """回答函数也在一次投递的期限之内（T9 决定 7）：到了期限还没答上，跟回答函数失败一样，立刻告诉
    提问方没有回答，问题确认掉、不重投——不等 broker 的确认时限关掉问题队列的通道。"""
    monkeypatch.setattr(receiving, "DELIVERY_DEADLINE", timedelta(seconds=0.5))
    thinker = NeverAnswers()
    inbox("world", on_message=answer_the_kitchen, on_question=thinker.on_question)
    await start_messaging()

    started = time.monotonic()
    answer = await ask(sender="operator", recipient="world", body="在吗？", timeout_seconds=5)

    assert not answer.answered and "时限" in answer.reason, answer
    assert time.monotonic() - started < 3, "提问方一直等到了自己的截止时刻"
    await eventually(_nothing_being_handled, timeout=5)
    await asyncio.sleep(1.0)
    assert (thinker.calls, thinker.cut_off) == (1, 1), "问题被重投、又答了一次"
    assert await broker.depth(f"questions_world_{LANE}") == 0


async def test_an_answer_still_thinking_when_the_asker_stops_waiting_is_cut_off_there(broker):
    """提问方的截止时刻比期限先到：答到那一刻为止，之后再答也没人等了。问题确认掉，不重投。"""
    thinker = NeverAnswers()
    inbox("world", on_message=answer_the_kitchen, on_question=thinker.on_question)
    await start_messaging()

    answer = await ask(sender="operator", recipient="world", body="在吗？", timeout_seconds=1)
    assert not answer.answered

    await eventually(_nothing_being_handled, timeout=2)
    await asyncio.sleep(1.0)
    assert (thinker.calls, thinker.cut_off) == (1, 1)
    assert await broker.depth(f"questions_world_{LANE}") == 0


async def test_a_question_whose_claim_is_held_up_past_its_time_is_acknowledged_unanswered(
    broker, test_db
):
    """领取那一步卡在库里（这里是领取要拿的那把锁被别人拿着）、过了提问方不再等的时刻：不再答，
    确认掉，不重投；不会一直挂着等锁，直到 broker 的确认时限关掉通道。"""
    from app.runtime.inflight import _lock_key

    thinker = NeverAnswers()
    inbox("world", on_message=answer_the_kitchen, on_question=thinker.on_question)
    await start_messaging()
    question_id = uuid.uuid4().hex
    async with test_db.connect() as holder:
        key = _lock_key(f"inbox:world@{LANE}", question_id)
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        try:
            answer = await ask(
                sender="operator",
                recipient="world",
                body="在吗？",
                timeout_seconds=1,
                message_id=question_id,
            )
            assert not answer.answered
            await eventually(_nothing_being_handled, timeout=2)
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})

    await asyncio.sleep(1.0)
    assert thinker.calls == 0
    assert await broker.depth(f"questions_world_{LANE}") == 0
