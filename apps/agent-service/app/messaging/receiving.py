"""接收一侧：拥有者开设收件箱、处理送来的消息和问题；以及定时送达到点时的那一步。

**收件箱由拥有者开设。** 一个 App 在自己的接线模块里调 :func:`inbox` 声明它拥有的
收件箱和处理函数；进程启动时 :func:`open_inboxes` 在本泳道建队列、开始消费。发送方
永远不会建收件箱，所以"这个名字开设过收件箱没有"就是"这条队列在不在"。队列不设
过期，拥有者不在线时消息一直留着。

**至少一次，按消息 id 去重。** broker 在消费者断开时会把没确认的消息重投，所以同一条
消息可能到两次。每个收件箱按 ``(inbox:<名字>@<泳道>, 消息 id)`` 在 ``runtime_inflight``
上占位（带泳道是因为 ppe 和 prod 共用一个库，同一个 id 在两条泳道各是各的一条）：处理成功过的不再处理；另一个进程正拿着它（租约还没过期）的，不丢，按剩下的
租约延时重新排回收件箱——那个进程要是半路死了，租约过期后由这里接管。接收方的处理
函数仍然可能在极端情况下看到同一条消息两次（处理完、还没来得及标记成功就崩了），
所以它拿到的 ``Message`` 带着 ``message_id``，自己据此去重。

**处理失败：有限次重试，然后进死信。** 普通消息的处理函数抛异常 → 按
:data:`PROCESSING_RETRY` 延时重投；次数用完 → 拒收，broker 把它送进本泳道的
``isolated_dead_letters_<泳道>``，原样保留消息体和消息头，可以查看、重放回原收件箱
（:mod:`app.messaging.dead_letters`，入口是 ``/admin/messaging/dead-letters*``）。

**问题不重试。** 问题的处理函数抛异常、返回空、或者收件箱不接受提问，都立刻给提问方
回一个"没有回答"。问题这条路径上的任何失败——回复发不出去、去重状态读写失败、消息
本身解不开——都只记一笔日志、确认掉：不重投，不进死信。提问方已经不等了（过了它带来
的截止时刻）的问题直接跳过。

**定时送达。** 定时队列里的消息到点才被送到这里：先看时刻到了没有——没到（延时被
broker 上限截成了几段）就按剩下的时长再排一段；到了，就在这一刻判断对方开设了收件箱
没有，投递或者记 ``not_delivered`` 并给原发送方发一条 ``not_delivered`` 告知。告知是原
发送方自己的消息被退回，所以发送方和接收方都是原发送方。告知的 id 由原消息 id 推出来，
这一步失败重试时再发的告知还是同一个 id，发送方按 id 去重。这一步失败同样按重试、
死信处理。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aio_pika.abc import AbstractIncomingMessage

from app.infra.rabbitmq import lane_queue, mq
from app.messaging.broker import (
    SCHEDULED,
    headers,
    hop_delay_ms,
    inbox_route,
    lane,
    lane_label,
    publish,
    reply_route,
)
from app.messaging.message import (
    Kind,
    Message,
    SendFailed,
    new_message,
    participant,
)
from app.messaging.record import Outcome
from app.messaging.sending import (
    ANSWER_BY_HEADER,
    REPLY_RK_HEADER,
    deliver,
    publish_recorded,
)
from app.runtime.inflight import claim_inflight, mark_failed, mark_succeeded
from app.runtime.propagation import bind_context, extract_context
from app.runtime.retry import DELIVERY_COUNT_HEADER, decide_retry
from app.runtime.wire import RetryPolicy

logger = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

# 一条消息最多处理 4 次（首次 + 3 次重试），退避 30 秒起、翻倍、封顶 10 分钟。
# 租约 15 分钟：比一次处理可能花的最长时间长，另一个进程在这期间不会接管它。
PROCESSING_RETRY = RetryPolicy(
    n=4,
    backoff="exponential",
    base_delay_ms=30_000,
    max_delay_ms=600_000,
    lease_ms=900_000,
)

# 另一个进程正拿着这条消息时，等它的租约到期后再多等这么久才重新看。
_LEASE_MARGIN_MS = 1_000

OnMessage = Callable[[Message], Awaitable[None]]
OnQuestion = Callable[[Message], Awaitable[str | None]]


@dataclass(frozen=True)
class InboxSpec:
    name: str
    on_message: OnMessage
    on_question: OnQuestion | None


INBOX_REGISTRY: dict[str, InboxSpec] = {}


def inbox(
    name: str, *, on_message: OnMessage, on_question: OnQuestion | None = None
) -> None:
    """声明本 App 拥有名为 ``name`` 的收件箱。在 App 的接线模块里调，进程启动时开设。

    ``on_message`` 处理普通消息和 ``not_delivered`` 告知，抛异常即处理失败（会重试）。
    ``on_question`` 回答问题，返回回答正文；返回 ``None`` 表示没有回答。不给它的
    收件箱不接受提问，问它的一律拿到"没有回答"。
    """
    participant(name)
    if name in INBOX_REGISTRY:
        raise RuntimeError(f"inbox {name!r} is already declared in this process")
    INBOX_REGISTRY[name] = InboxSpec(name, on_message, on_question)


def clear_inboxes() -> None:
    INBOX_REGISTRY.clear()


# (channel, queue, consumer_tag)，停的时候逐个取消。
_consumers: list[tuple[Any, Any, str]] = []


async def _consume(route, handler) -> None:
    channel = await mq.open_channel()
    queue = await channel.get_queue(lane_queue(route.queue, lane()))
    tag = await queue.consume(handler)
    _consumers.append((channel, queue, tag))
    logger.info("messaging: consuming %s", queue.name)


async def start_receiving() -> None:
    """开设本进程声明的全部收件箱并开始消费；同时消费本泳道的定时队列。"""
    await mq.declare_route(SCHEDULED, lane=lane())
    await _consume(SCHEDULED, _on_scheduled)
    for spec in INBOX_REGISTRY.values():
        route = inbox_route(spec.name)
        await mq.declare_route(route, lane=lane())
        await _consume(route, _inbox_handler(spec))


async def stop_receiving() -> None:
    consumers, _consumers[:] = list(_consumers), []
    for channel, queue, tag in consumers:
        try:
            await queue.cancel(tag)
        except Exception:
            logger.warning("messaging: cancel %s failed", queue.name, exc_info=True)
        if not channel.is_closed:
            try:
                await channel.close()
            except Exception:
                logger.warning("messaging: close channel failed", exc_info=True)


# ---------------------------------------------------------------------------
# 一条消息的处理：去重、重试、死信
# ---------------------------------------------------------------------------


async def _run_once(
    message: Message,
    received: dict[str, Any],
    *,
    route,
    edge_id: str,
    run: Callable[[Message, dict[str, Any]], Awaitable[None]],
) -> None:
    """按消息 id 去重后跑 ``run``；普通消息失败按 :data:`PROCESSING_RETRY` 重投，用完就抛。

    调用方把这一步包在 ``incoming.process(requeue=False)`` 里：这里抛出去，broker 按队列
    参数把消息送进本泳道的死信队列。重投的发布没被确认时同样抛——宁可进死信，不能丢。

    **问题不走这条重试路径。** 问题处理中的任何失败都只记一笔、确认掉，不重投也不进
    死信：提问方那边早就按"没有回答"处理了。别的进程正拿着的问题也不再排回去。

    每次占位用一个只属于这一次的标记：租约过期被别人接管之后，这里的成功或失败都
    不再改那一行（:func:`app.runtime.inflight.mark_failed` 的 ``worker_id`` 条件），
    也不再重投——消息已经归接管的那一方负责。
    """
    is_question = message.kind is Kind.QUESTION
    claim_token = f"{WORKER_ID}#{uuid.uuid4().hex[:12]}"
    claim = await claim_inflight(
        edge_id=edge_id,
        idempotent_key=message.message_id,
        data_table=route.queue,
        worker_id=claim_token,
        lease_ms=PROCESSING_RETRY.lease_ms,
        trace_id=extract_context(received).trace_id,
    )
    if claim.action == "skip":
        if claim.locked_until is not None and not is_question:
            wait_ms = hop_delay_ms(claim.locked_until, datetime.now(UTC))
            await publish(
                route,
                message.to_json(),
                headers=received,
                delay_ms=wait_ms + _LEASE_MARGIN_MS,
            )
            logger.info(
                "messaging: %s %s is held by another worker; re-queued",
                edge_id,
                message.message_id,
            )
        return

    try:
        await run(message, received)
    except Exception as exc:
        still_mine = await mark_failed(
            edge_id=edge_id,
            idempotent_key=message.message_id,
            last_error=f"{type(exc).__name__}: {exc}",
            worker_id=claim_token,
        )
        if is_question or not still_mine:
            logger.exception(
                "messaging: %s %s failed; %s",
                edge_id,
                message.message_id,
                "questions are never retried"
                if is_question
                else "its claim was taken over, the new holder owns the outcome",
            )
            return
        decision = decide_retry(headers=received, policy=PROCESSING_RETRY)
        if decision.action != "retry":
            logger.exception(
                "messaging: %s %s failed for good; dead-lettered",
                edge_id,
                message.message_id,
            )
            raise
        logger.warning(
            "messaging: %s %s failed (%r); retry %d in %d ms",
            edge_id,
            message.message_id,
            exc,
            decision.attempt,
            decision.delay_ms,
        )
        await publish(
            route,
            message.to_json(),
            headers={**received, DELIVERY_COUNT_HEADER: decision.attempt},
            delay_ms=decision.delay_ms,
        )
        return
    if not await mark_succeeded(
        edge_id=edge_id, idempotent_key=message.message_id, worker_id=claim_token
    ):
        logger.warning(
            "messaging: %s %s finished after its claim was taken over",
            edge_id,
            message.message_id,
        )


def _edge(base: str) -> str:
    """去重状态的 edge：带上部署泳道。ppe 和 prod 共用 runtime_inflight 所在的库。"""
    return f"{base}@{lane_label()}"


def _is_question(body: bytes) -> bool:
    """只看 ``kind``，解不开就不是问题。用来在完整解码之前决定走哪条失败处理。"""
    try:
        return json.loads(body).get("kind") == str(Kind.QUESTION)
    except Exception:
        return False


def _inbox_handler(spec: InboxSpec):
    route = inbox_route(spec.name)
    edge_id = _edge(f"inbox:{spec.name}")

    async def run(message: Message, received: dict[str, Any]) -> None:
        if message.kind is Kind.QUESTION:
            await _answer(spec, message, received)
        else:
            await spec.on_message(message)

    async def process(incoming: AbstractIncomingMessage) -> None:
        received = dict(incoming.headers or {})
        message = Message.from_json(json.loads(incoming.body))
        async with bind_context(extract_context(received)):
            await _run_once(message, received, route=route, edge_id=edge_id, run=run)

    async def handler(incoming: AbstractIncomingMessage) -> None:
        async with incoming.process(requeue=False, ignore_processed=True):
            if not _is_question(incoming.body):
                await process(incoming)
                return
            # 问题：整条路径上的任何失败（包括去重状态读写、解码）都在这里收住并确认。
            try:
                await process(incoming)
            except Exception:
                logger.exception(
                    "messaging: a question to %s failed; acknowledged, never redelivered",
                    spec.name,
                )

    return handler


async def _answer(spec: InboxSpec, question: Message, received: dict[str, Any]) -> None:
    """回答一个问题，或者告诉提问方没有回答。这里不抛异常，所以问题永远不会被重试。"""
    reply_rk = received.get(REPLY_RK_HEADER)
    answer_by = received.get(ANSWER_BY_HEADER)
    if not isinstance(reply_rk, str) or not isinstance(answer_by, str):
        logger.warning(
            "messaging: question %s carries no reply address; skipped",
            question.message_id,
        )
        return
    if datetime.now(UTC) >= datetime.fromisoformat(answer_by):
        logger.info(
            "messaging: question %s arrived after its asker stopped waiting; skipped",
            question.message_id,
        )
        return

    text: str | None = None
    reason: str | None = None
    if spec.on_question is None:
        reason = "对方不接受提问"
    else:
        try:
            text = await spec.on_question(question)
        except Exception as exc:
            logger.exception("messaging: answering %s failed", question.message_id)
            reason = f"对方回答时失败：{type(exc).__name__}"
        else:
            if not isinstance(text, str) or not text.strip():
                text, reason = None, "对方没有给出回答"

    route = reply_route(reply_rk)
    if text is not None:
        answer = new_message(
            sender=spec.name, recipient=question.sender, body=text, kind=Kind.ANSWER
        )
        try:
            await publish_recorded(
                answer,
                route,
                {"in_reply_to": question.message_id, "message": answer.to_json()},
                headers=headers(),
                done=Outcome.DELIVERED,
                in_reply_to=question.message_id,
            )
            return
        except SendFailed as exc:
            # 回答也是一条消息：没记下来就不算发出。告诉提问方没有回答，不重投问题。
            logger.exception("messaging: answer to %s not sent", question.message_id)
            reason = f"回答没能发出：{exc}"
    await publish(
        route,
        {"in_reply_to": question.message_id, "message": None, "reason": reason},
        headers=headers(),
    )


# ---------------------------------------------------------------------------
# 定时送达
# ---------------------------------------------------------------------------


async def _on_scheduled(incoming: AbstractIncomingMessage) -> None:
    """定时队列上的一条：没到时刻就再排一段；到了就在这一刻投递。

    "再排一段"不经过去重：同一条消息分段时每一段都会到这里一次，它们不是重复。
    """
    async with incoming.process(requeue=False, ignore_processed=True):
        received = dict(incoming.headers or {})
        message = Message.from_json(json.loads(incoming.body))
        now = datetime.now(UTC)
        if message.time > now:
            await publish(
                SCHEDULED,
                message.to_json(),
                headers=received,
                delay_ms=hop_delay_ms(message.time, now),
            )
            return
        async with bind_context(extract_context(received)):
            await _run_once(
                message,
                received,
                route=SCHEDULED,
                edge_id=_edge("messaging:scheduled"),
                run=_deliver_due,
            )


def _notice_id(message: Message) -> str:
    """一条定时消息的"没有送达"告知的 id：由原消息 id 确定地推出来，重试时不变。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"messaging:not_delivered:{message.message_id}").hex


async def _deliver_due(message: Message, received: dict[str, Any]) -> None:
    delivery = await deliver(message)
    if delivery.delivered:
        return
    # 这是发送方自己的消息被退回：发送方和接收方都是它。填成没开设收件箱的那一方，
    # 看起来就像对方发来了一条消息，而对方根本不在。
    notice = new_message(
        sender=message.sender,
        recipient=message.sender,
        kind=Kind.NOT_DELIVERED,
        message_id=_notice_id(message),
        body=(
            f"你定在 {message.time.isoformat()} 发给 {message.recipient} 的消息没有送达"
            f"（{delivery.reason}）。原文：{message.body}"
        ),
    )
    await deliver(notice)
