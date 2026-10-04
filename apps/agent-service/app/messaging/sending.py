"""三种操作的发送一侧：发给具名参与者、提问并同步等回答、指定时刻送达。

发送方只知道接收方的名字。它不知道对方用什么实现、在哪个进程、在不在线——
它能知道的只有"这个名字在这条泳道开设过收件箱没有"。

* :func:`send` —— 对方没开设收件箱：不投递、记一行 ``not_delivered``、结果里
  ``delivered=False``。开设过：记一行 ``delivered`` 并投进收件箱（对方不在线也
  一直保留，等它上线）。消息的时间默认是发出那一刻，发送方也可以给它所说的事发生的那一刻。
* :func:`ask` —— 投进对方收件箱旁边的问题队列（不进收件箱，所以不排在对方正在处理的普通
  消息后面），然后在本进程的私有回复队列上等。问题队列不在（对方没开设收件箱，或者还跑着
  没有问题队列的旧代码）就跟 ``send`` 一样不投递、记 ``not_delivered``。对方不在线、处理
  失败、没给回答、超时，一律拿到 ``Answer(text=None, reason=...)``，并记一行 ``no_answer``。
  **不自动重试**：问题只发一次。
* :func:`send_at` —— 记一行 ``scheduled``，把消息放进本泳道的定时队列。到点时由
  :mod:`app.messaging.receiving` 判断对方开设了收件箱没有，再决定投递还是记
  ``not_delivered`` 并告知发送方。时长没有业务上限，超过 broker 延时上限的部分分段。

记录是发送的一部分（:func:`publish_recorded`）：先记 ``sending``、再发、再记结果，三步
各自提交。任何一步失败都抛带消息 id 的 :class:`SendFailed`；调用方要重试就把这个 id
传回来（三个操作都收 ``message_id``），接收方按 id 去重。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from aio_pika.abc import AbstractIncomingMessage

from app.infra.rabbitmq import Route, mq
from app.messaging.broker import (
    SCHEDULED,
    headers,
    hop_delay_ms,
    inbox_route,
    lane,
    opened,
    publish,
    question_route,
    reply_route,
)
from app.messaging.message import (
    Answer,
    Delivery,
    Kind,
    Message,
    SendFailed,
    new_message,
)
from app.messaging.record import Outcome, record

logger = logging.getLogger(__name__)

NO_INBOX = "对方没有开设收件箱"

# 提问随消息头带过去的两项：回答发回哪里、提问方最多等到什么时候。
REPLY_RK_HEADER = "x-reply-rk"
ANSWER_BY_HEADER = "x-answer-by"


async def publish_recorded(
    message: Message,
    route: Route,
    body: dict[str, Any],
    *,
    headers: dict[str, Any],
    done: Outcome,
    delay_ms: int | None = None,
    in_reply_to: str | None = None,
) -> None:
    """记 ``sending`` → 发给 broker 等确认 → 记 ``done``，三步各自提交。

    第一步写不进去就不发。broker 没确认时补记 ``unconfirmed``（补记本身失败也不要紧，
    ``sending`` 那一行已经在了）。任何一步失败都抛带 ``message.message_id`` 的
    ``SendFailed``。
    """
    await record(message, Outcome.SENDING, in_reply_to=in_reply_to)
    try:
        await publish(route, body, headers=headers, delay_ms=delay_ms)
    except SendFailed as exc:
        try:
            await record(
                message, Outcome.UNCONFIRMED, reason=str(exc), in_reply_to=in_reply_to
            )
        except SendFailed:
            logger.exception(
                "messaging: could not record %s as unconfirmed", message.message_id
            )
        raise SendFailed(str(exc), message_id=message.message_id) from exc
    await record(message, done, in_reply_to=in_reply_to)


async def send(
    *,
    sender: str,
    recipient: str,
    body: str,
    message_id: str | None = None,
    time: datetime | None = None,
) -> Delivery:
    """立即发给 ``recipient``。

    ``time`` 是这条消息的时间，必须带时区，不给就是现在。消息说的是早先发生的事、又要按发生的
    先后排在对方那里时给它：比如补发一条当时没发出去的，给它原来的时间，发出那一刻会让它排到
    之后才发生的事后面。它只是消息上的时间，不推迟送达；要到某一刻才送达用 :func:`send_at`。
    """
    if time is not None and time.tzinfo is None:
        raise ValueError("send needs a timezone-aware time")
    message = new_message(
        sender=sender,
        recipient=recipient,
        body=body,
        kind=Kind.MESSAGE,
        time=time,
        message_id=message_id,
    )
    return await deliver(message)


async def deliver(message: Message) -> Delivery:
    """把一条已经造好的消息投进对方的收件箱，或者记下它没送到。"""
    if not await opened(inbox_route(message.recipient)):
        await record(message, Outcome.NOT_DELIVERED, reason=NO_INBOX)
        return Delivery(message.message_id, delivered=False, reason=NO_INBOX)
    await publish_recorded(
        message,
        inbox_route(message.recipient),
        message.to_json(),
        headers=headers(),
        done=Outcome.DELIVERED,
    )
    return Delivery(message.message_id, delivered=True)


async def send_at(
    *,
    sender: str,
    recipient: str,
    body: str,
    at: datetime,
    message_id: str | None = None,
) -> str:
    """排一条 ``at`` 时刻送达的消息，返回消息 id。``at`` 必须带时区。"""
    if at.tzinfo is None:
        raise ValueError("send_at needs a timezone-aware time")
    if os.getenv("RABBITMQ_DISABLE_DELAYED") == "1":
        raise SendFailed(
            "scheduled delivery needs the x-delayed-message exchange, which is "
            "disabled in this process (RABBITMQ_DISABLE_DELAYED=1)"
        )
    message = new_message(
        sender=sender,
        recipient=recipient,
        body=body,
        kind=Kind.MESSAGE,
        time=at,
        message_id=message_id,
    )
    await publish_recorded(
        message,
        SCHEDULED,
        message.to_json(),
        headers=headers(),
        done=Outcome.SCHEDULED,
        delay_ms=hop_delay_ms(at, datetime.now(UTC)),
    )
    return message.message_id


# ---------------------------------------------------------------------------
# 提问
# ---------------------------------------------------------------------------

# 本进程的回复队列：第一次提问时建，进程（连接）在它就在。
_reply_rk: str | None = None
_reply_channel: Any = None
_reply_lock: asyncio.Lock | None = None
_waiting: dict[str, asyncio.Future] = {}


async def _reply_address() -> str:
    global _reply_rk, _reply_channel, _reply_lock
    if _reply_lock is None:
        _reply_lock = asyncio.Lock()
    async with _reply_lock:
        if _reply_rk is None:
            rk = f"messaging.reply.{uuid.uuid4().hex}"
            channel = await mq.open_channel()
            queue = await mq.declare_private_queue(channel, reply_route(rk), lane())
            await queue.consume(_on_reply, no_ack=True)
            _reply_channel, _reply_rk = channel, rk
        return _reply_rk


async def _on_reply(incoming: AbstractIncomingMessage) -> None:
    try:
        reply = json.loads(incoming.body)
        waiter = _waiting.get(reply["in_reply_to"])
    except Exception:
        logger.exception("messaging: unreadable reply dropped")
        return
    if waiter is not None and not waiter.done():
        waiter.set_result(reply)


async def close_replies() -> None:
    """停掉本进程的回复队列，并让还在等的提问立刻拿到"没有回答"。"""
    global _reply_rk, _reply_channel, _reply_lock
    for waiter in _waiting.values():
        if not waiter.done():
            waiter.set_result({"message": None, "reason": "提问方所在的进程正在停止"})
    channel, _reply_channel, _reply_rk = _reply_channel, None, None
    _reply_lock = None
    if channel is not None and not channel.is_closed:
        try:
            await channel.close()
        except Exception:
            logger.warning("messaging: closing the reply channel failed", exc_info=True)


async def ask(
    *,
    sender: str,
    recipient: str,
    body: str,
    timeout_seconds: float,
    message_id: str | None = None,
) -> Answer:
    """向 ``recipient`` 提问，最多等 ``timeout_seconds`` 秒，返回回答或"没有回答"。

    ``message_id`` 只在重试一次抛了 ``SendFailed`` 的提问时给，沿用原来的 id。要知道的
    是：**用原 id 重试一个已经被回答过的问题，结果是"没有回答"，对方的业务处理不会再
    执行一次。** 原来那次的回答发给的是原来那次等待，而 ``SendFailed`` 抛出时那次等待
    已经撤掉；接收方按 id 认出这个问题处理过，不会再答。回答不做保存和重放。
    """
    question = new_message(
        sender=sender,
        recipient=recipient,
        body=body,
        kind=Kind.QUESTION,
        message_id=message_id,
    )
    route = question_route(recipient)
    if not await opened(route):
        await record(question, Outcome.NOT_DELIVERED, reason=NO_INBOX)
        return Answer(question.message_id, None, NO_INBOX)

    reply_rk = await _reply_address()
    waiter: asyncio.Future = asyncio.get_running_loop().create_future()
    _waiting[question.message_id] = waiter
    answer_by = datetime.now(UTC) + timedelta(seconds=timeout_seconds)
    try:
        await publish_recorded(
            question,
            route,
            question.to_json(),
            headers=headers(
                {REPLY_RK_HEADER: reply_rk, ANSWER_BY_HEADER: answer_by.isoformat()}
            ),
            done=Outcome.DELIVERED,
        )
        try:
            reply = await asyncio.wait_for(waiter, timeout=timeout_seconds)
        except TimeoutError:
            reply = {"message": None, "reason": f"{timeout_seconds:g} 秒内没有回答"}
    finally:
        _waiting.pop(question.message_id, None)

    if reply.get("message"):
        answer = Message.from_json(reply["message"])
        return Answer(question.message_id, answer.body)
    reason = str(reply.get("reason") or "没有回答")
    await record(question, Outcome.NO_ANSWER, reason=reason)
    return Answer(question.message_id, None, reason)
