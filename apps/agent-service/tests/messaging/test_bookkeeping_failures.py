"""记账和转交环节失败：不是消息本身的问题，所以不进死信。

通信机制的原则：**进死信的原因只有一个——业务处理函数本身失败，有限次重试已经用完，拥有者
也没要求不限次数重试。** 领取、标记成功或失败、写记录、租约冲突后的重投、延迟重投的发布、
定时转交、分段发布，这些环节失败都是基础设施失败：

* 普通消息和最新唤醒（拥有者要求不限次数重试的那种）：延迟后放回原队列，之后照常处理成功；
  拒收时 ``requeue=False`` 一次都不出现。
* 问题：确认掉，回答函数累计至多执行一次。

每个环节对三类消息各注入一次失败（问题不走定时通道，也没有"失败重投"，这两处对问题用的是
它自己那条路上对应的一步：回复发布、回答函数本身失败）。跑在真 broker + 真 Postgres 上。
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.infra.rabbitmq import ISOLATED_DEAD_LETTERS, Route, mq
from app.messaging.lifecycle import start_messaging
from app.messaging.message import Kind, SendFailed, new_message
from app.messaging.receiving import inbox
from app.messaging.sending import ask, send, send_at
from app.runtime.retry import RetryPolicy

from .conftest import LANE
from .helpers import eventually

pytestmark = pytest.mark.usefixtures("messaging_db")

LATEST = "最新唤醒"
FAIL_ONCE = "业务失败一次"


class World:
    """一个收件箱：普通消息照单全收（带 FAIL_ONCE 的第一次抛错），问题照常回答。"""

    def __init__(self) -> None:
        self.handled: list[str] = []
        self.attempts: dict[str, int] = {}
        self.answers = 0

    async def on_message(self, message) -> None:
        self.attempts[message.body] = self.attempts.get(message.body, 0) + 1
        if FAIL_ONCE in message.body and self.attempts[message.body] == 1:
            raise RuntimeError("业务处理失败一次")
        self.handled.append(message.body)

    async def on_question(self, message) -> str:
        self.answers += 1
        return "在。"

    async def retry_without_limit(self, message):
        return timedelta(seconds=1) if LATEST in message.body else None


@pytest.fixture
def rejects_to_dead_letters(monkeypatch) -> list[bool]:
    """记下每一次 ``reject(requeue=False)``：它就是"送进死信"。"""
    from aio_pika.message import IncomingMessage

    real = IncomingMessage.reject
    seen: list[bool] = []

    async def reject(self, requeue: bool = False):
        if not requeue:
            seen.append(True)
        return await real(self, requeue=requeue)

    monkeypatch.setattr(IncomingMessage, "reject", reject)
    return seen


@pytest.fixture
def world(broker, monkeypatch, rejects_to_dead_letters) -> World:
    from app.messaging import receiving

    monkeypatch.setattr(
        receiving,
        "PROCESSING_RETRY",
        RetryPolicy(n=3, backoff="linear", base_delay_ms=200, max_delay_ms=300, lease_ms=1_000),
    )
    monkeypatch.setattr(receiving, "PUT_BACK_BASE_SECONDS", 0.1)
    monkeypatch.setattr(receiving, "PUT_BACK_CAP_SECONDS", 0.4)
    w = World()
    inbox(
        "world",
        on_message=w.on_message,
        on_question=w.on_question,
        retry_without_limit=w.retry_without_limit,
    )
    return w


def _fail_once(monkeypatch, module, name, *, when=lambda *a, **kw: True, times: int = 1):
    """把 ``module.name`` 换成：头 ``times`` 次满足 ``when`` 时抛错，其余照常。

    交回一个"注入完了没有"的查询。
    """
    real = getattr(module, name)
    state = {"left": times}

    async def wrapper(*args, **kwargs):
        if state["left"] and when(*args, **kwargs):
            state["left"] -= 1
            if name in ("publish", "deliver", "record"):
                raise SendFailed(f"{name} failed (injected)")
            raise RuntimeError(f"{name} failed (injected)")
        return await real(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)
    return lambda: state["left"] == 0


async def _nothing_dead_lettered(broker, rejects_to_dead_letters) -> None:
    assert rejects_to_dead_letters == [], "有消息被拒收进了死信"
    assert await broker.depth(f"{ISOLATED_DEAD_LETTERS}_{LANE}") == 0


async def _handled(world: World, body: str) -> None:
    await eventually(lambda: body in world.handled, timeout=15)


async def _asked_once(world: World, broker) -> None:
    """问一次；回答函数至多执行一次，问题确认掉、不回队列。"""
    await ask(sender="operator", recipient="world", body="在吗？", timeout_seconds=2)
    await asyncio.sleep(1.0)
    assert world.answers <= 1
    assert await broker.depth(f"questions_world_{LANE}") == 0


MESSAGE_KINDS = ["plain", "latest"]


def _body(kind: str, text: str) -> str:
    return f"{LATEST}：{text}" if kind == "latest" else text


# ---------------------------------------------------------------------------
# 领取
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", MESSAGE_KINDS)
async def test_claiming_fails(world, broker, monkeypatch, rejects_to_dead_letters, kind):
    from app.messaging import receiving

    injected = _fail_once(monkeypatch, receiving, "claim_inflight")
    await start_messaging()
    body = _body(kind, "领取失败。")
    await send(sender="operator", recipient="world", body=body)

    await _handled(world, body)
    assert injected()
    await _nothing_dead_lettered(broker, rejects_to_dead_letters)


async def test_claiming_fails_for_a_question(world, broker, monkeypatch, rejects_to_dead_letters):
    from app.messaging import receiving

    injected = _fail_once(monkeypatch, receiving, "claim_inflight")
    await start_messaging()

    await _asked_once(world, broker)
    assert injected()
    await _nothing_dead_lettered(broker, rejects_to_dead_letters)


# ---------------------------------------------------------------------------
# 标记成功
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", MESSAGE_KINDS)
async def test_marking_success_fails(world, broker, monkeypatch, rejects_to_dead_letters, test_db, kind):
    from app.messaging import receiving

    injected = _fail_once(monkeypatch, receiving, "mark_succeeded")
    await start_messaging()
    body = _body(kind, "标记成功失败。")
    delivery = await send(sender="operator", recipient="world", body=body)

    async def settled():
        async with test_db.begin() as conn:
            state = (
                await conn.execute(
                    text("SELECT state FROM runtime_inflight WHERE idempotent_key = :k"),
                    {"k": delivery.message_id},
                )
            ).scalar()
        return state == "succeeded"

    await eventually(settled, timeout=15)
    assert injected()
    assert body in world.handled
    await _nothing_dead_lettered(broker, rejects_to_dead_letters)


async def test_marking_a_question_handled_fails(world, broker, monkeypatch, rejects_to_dead_letters):
    from app.messaging import receiving

    injected = _fail_once(monkeypatch, receiving, "mark_succeeded")
    await start_messaging()

    await _asked_once(world, broker)
    assert injected()
    await _nothing_dead_lettered(broker, rejects_to_dead_letters)


# ---------------------------------------------------------------------------
# 标记失败（业务处理失败了一次之后）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", MESSAGE_KINDS)
async def test_marking_failure_fails(world, broker, monkeypatch, rejects_to_dead_letters, kind):
    from app.messaging import receiving

    injected = _fail_once(monkeypatch, receiving, "mark_failed")
    await start_messaging()
    body = _body(kind, f"{FAIL_ONCE}，标记失败也失败。")
    await send(sender="operator", recipient="world", body=body)

    await _handled(world, body)
    assert injected()
    await _nothing_dead_lettered(broker, rejects_to_dead_letters)


async def test_answering_itself_fails_for_a_question(
    world, broker, monkeypatch, rejects_to_dead_letters
):
    """问题没有"标记失败"那一步：它对应的是回答函数本身失败。确认掉，只答过一次。"""

    async def broken(message):
        world.answers += 1
        raise RuntimeError("回答失败")

    world.on_question = broken
    from app.messaging.receiving import INBOX_REGISTRY, InboxSpec

    spec = INBOX_REGISTRY["world"]
    INBOX_REGISTRY["world"] = InboxSpec(**{**spec.__dict__, "on_question": broken})
    await start_messaging()

    await _asked_once(world, broker)
    assert world.answers == 1
    await _nothing_dead_lettered(broker, rejects_to_dead_letters)


# ---------------------------------------------------------------------------
# 写记录（定时转交时那一行；问题是回答那一行）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", MESSAGE_KINDS)
async def test_recording_the_scheduled_delivery_fails(
    world, broker, monkeypatch, rejects_to_dead_letters, kind
):
    from app.messaging import sending

    await start_messaging()
    body = _body(kind, "转交时记录失败。")
    await send_at(
        sender="operator",
        recipient="world",
        body=body,
        at=datetime.now(UTC) + timedelta(seconds=1.0),
    )
    # 排好之后才注入：坏的是到点转交那几次，比有限次重试的上限还多。
    injected = _fail_once(monkeypatch, sending, "record", times=5)

    await _handled(world, body)
    assert injected()
    await _nothing_dead_lettered(broker, rejects_to_dead_letters)


async def test_recording_the_answer_fails(world, broker, monkeypatch, rejects_to_dead_letters):
    from app.messaging import sending

    injected = _fail_once(
        monkeypatch, sending, "record", when=lambda message, outcome, **kw: message.kind is Kind.ANSWER
    )
    await start_messaging()

    await _asked_once(world, broker)
    assert injected()
    assert world.answers == 1
    await _nothing_dead_lettered(broker, rejects_to_dead_letters)


# ---------------------------------------------------------------------------
# 租约冲突后的重投
# ---------------------------------------------------------------------------


async def _held_by_a_dead_peer(test_db, message_id: str, seconds: float) -> None:
    async with test_db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runtime_inflight (edge_id, idempotent_key, data_table, "
                "state, attempts, locked_until, worker_id) VALUES "
                f"('inbox:world@{LANE}', :k, 'inbox_world', 'processing', 1, "
                f"now() + interval '{seconds} seconds', 'dead-peer:1')"
            ),
            {"k": message_id},
        )


@pytest.mark.parametrize("kind", MESSAGE_KINDS)
async def test_requeueing_behind_a_live_claim_fails(
    world, broker, monkeypatch, rejects_to_dead_letters, test_db, kind
):
    from app.messaging import receiving

    body = _body(kind, "租约冲突后重投失败。")
    message = new_message(sender="operator", recipient="world", body=body, kind=Kind.MESSAGE)
    await _held_by_a_dead_peer(test_db, message.message_id, 1.0)
    injected = _fail_once(
        monkeypatch,
        receiving,
        "publish",
        when=lambda route, body, *, headers, delay_ms=None: route.rk == "inbox.world"
        and delay_ms
        and not headers.get("x-delivery-count"),
    )
    await start_messaging()
    assert await mq.publish_with_confirm(
        Route("inbox_world", "inbox.world", isolated=True), message.to_json(), lane=LANE
    )

    await _handled(world, body)
    assert injected()
    await _nothing_dead_lettered(broker, rejects_to_dead_letters)


async def test_a_question_behind_a_live_claim(world, broker, rejects_to_dead_letters, test_db):
    """问题撞上别人还活着的占位：不排回去，确认掉。"""
    await start_messaging()
    question = new_message(sender="operator", recipient="world", body="在吗？", kind=Kind.QUESTION)
    await _held_by_a_dead_peer(test_db, question.message_id, 30.0)

    answer = await ask(
        sender="operator",
        recipient="world",
        body="在吗？",
        timeout_seconds=2,
        message_id=question.message_id,
    )

    assert not answer.answered
    assert world.answers == 0
    assert await broker.depth(f"questions_world_{LANE}") == 0
    await _nothing_dead_lettered(broker, rejects_to_dead_letters)


# ---------------------------------------------------------------------------
# 延迟重投的发布（问题没有重投：对应的是回复的发布）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", MESSAGE_KINDS)
async def test_publishing_the_retry_copy_fails(
    world, broker, monkeypatch, rejects_to_dead_letters, kind
):
    from app.messaging import receiving

    injected = _fail_once(
        monkeypatch,
        receiving,
        "publish",
        when=lambda route, body, *, headers, delay_ms=None: bool(headers.get("x-delivery-count")),
    )
    await start_messaging()
    body = _body(kind, f"{FAIL_ONCE}，重投发不出去。")
    await send(sender="operator", recipient="world", body=body)

    await _handled(world, body)
    assert injected()
    await _nothing_dead_lettered(broker, rejects_to_dead_letters)


async def test_publishing_the_reply_fails(world, broker, monkeypatch, rejects_to_dead_letters):
    from app.messaging import receiving

    injected = _fail_once(
        monkeypatch,
        receiving,
        "publish",
        when=lambda route, body, *, headers, delay_ms=None: route.rk.startswith("messaging.reply."),
    )
    from app.messaging import sending

    _fail_once(
        monkeypatch,
        sending,
        "publish",
        when=lambda route, body, *, headers, delay_ms=None: route.rk.startswith("messaging.reply."),
    )
    await start_messaging()

    await _asked_once(world, broker)
    assert injected() or world.answers == 1
    assert world.answers == 1
    await _nothing_dead_lettered(broker, rejects_to_dead_letters)


# ---------------------------------------------------------------------------
# 定时转交、分段发布（问题不走定时通道）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", MESSAGE_KINDS)
async def test_handing_over_a_due_message_fails(
    world, broker, monkeypatch, rejects_to_dead_letters, kind
):
    """到点转交连着失败好几次（比有限次重试的上限还多），照样一直重试，最后送到。"""
    from app.messaging import receiving

    real = receiving.deliver
    failures = {"left": 5}

    async def deliver(message):
        if failures["left"]:
            failures["left"] -= 1
            raise SendFailed("hand-over failed (injected)")
        return await real(message)

    monkeypatch.setattr(receiving, "deliver", deliver)
    await start_messaging()
    body = _body(kind, "到点转交失败五次。")
    await send_at(
        sender="operator",
        recipient="world",
        body=body,
        at=datetime.now(UTC) + timedelta(seconds=0.5),
    )

    await _handled(world, body)
    assert failures["left"] == 0
    await _nothing_dead_lettered(broker, rejects_to_dead_letters)


@pytest.mark.parametrize("kind", MESSAGE_KINDS)
async def test_publishing_the_next_hop_fails(
    world, broker, monkeypatch, rejects_to_dead_letters, kind
):
    from app.messaging import broker as broker_mod
    from app.messaging import receiving

    monkeypatch.setattr(broker_mod, "DELAY_LIMIT_MS", 400)
    injected = _fail_once(
        monkeypatch,
        receiving,
        "publish",
        when=lambda route, body, *, headers, delay_ms=None: route.rk == "messaging.scheduled",
    )
    await start_messaging()
    body = _body(kind, "分段发布失败。")
    await send_at(
        sender="operator",
        recipient="world",
        body=body,
        at=datetime.now(UTC) + timedelta(seconds=1.5),
    )

    await _handled(world, body)
    assert injected()
    await _nothing_dead_lettered(broker, rejects_to_dead_letters)


# ---------------------------------------------------------------------------
# 原有的契约：业务处理失败到头，照样进死信
# ---------------------------------------------------------------------------


async def test_a_plain_message_whose_handler_keeps_failing_still_goes_to_dead_letters(
    world, broker, rejects_to_dead_letters
):
    async def always_fails(message):
        raise RuntimeError("业务处理一直失败")

    from app.messaging.receiving import INBOX_REGISTRY, InboxSpec

    spec = INBOX_REGISTRY["world"]
    INBOX_REGISTRY["world"] = InboxSpec(**{**spec.__dict__, "on_message": always_fails})
    await start_messaging()
    await send(sender="operator", recipient="world", body="一直失败。")

    await eventually(lambda: broker.depth(f"{ISOLATED_DEAD_LETTERS}_{LANE}"), timeout=15)
    assert rejects_to_dead_letters == [True]
