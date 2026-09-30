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

**拥有者开设时可以多声明三件事**（:func:`inbox`）：

* ``processing_timeout`` —— 一条消息最多处理多久。超过就取消，算一次处理失败（按上面的
  重试、死信处理）；这条消息的去重占位租约相应放长到它之上
  （:data:`LEASE_OVER_TIMEOUT_MS`），所以处理还没完时到的重复副本不会把它当成"前一个
  进程死了"接管过去。不声明就是默认的 15 分钟租约、不限时。
* ``one_at_a_time`` —— 一次只处理一条：这个收件箱的消费通道 prefetch 为 1，broker 在上一
  条确认之前不送下一条。租约从真正开始处理那一刻起算，排队等的那几条不占租约。
* ``on_open`` —— 收件箱开设（队列建好）之后、开始消费之前调一次。拥有者在这里按自己的
  状态做启动时该做的事，可以往自己的收件箱里发消息，它们等这一步结束才被处理。它抛
  异常，启动就失败。
* ``on_final_failure`` —— 一条普通消息最后一次重试也失败、即将进死信时调一次，带着那条
  消息和最后那次的异常。消息照常进死信（人工可查看、可重放）；钩子只是让拥有者知道这件
  事并自己做点什么。钩子本身失败只记一笔日志，消息照样进死信。问题不走这条路径。
* ``consume_while`` —— 只在持有它（一个异步上下文，比如一把跨进程的独占锁）期间消费。
  开设时不在启动流程里等它：队列照常建好，启动照常返回，后台等到进了这个上下文，才跑
  ``on_open``、开始消费；停止时等正在处理的消息处理完（见下面"停下时"）之后才退出这个
  上下文。进了上下文之后 ``on_open`` 失败，就退出上下文、隔 :data:`OPEN_RETRY_SECONDS`
  再来一次——这时启动早已返回，失败不能再靠让启动失败来暴露。

**停下时正在处理的消息**（:func:`stop_receiving`）：先取消消费者（不再有新消息进来），等正在
处理的那几条处理完、照常确认，最多等 :data:`STOP_GRACE_SECONDS`。等不完的，先关通道再取消：
通道关着，取消时不会拒收进死信，broker 把没确认的消息放回原队列；取消的同时放开它的去重
占位，重投的那一份马上有人接，不用等租约过期。声明了 ``consume_while`` 的收件箱：还在等着
进上下文的，停止时直接放弃等待；已经在消费的，等上面这些都做完才退出上下文。

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

import asyncio
import json
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
# 租约 15 分钟：另一个进程在这期间不会接管它。处理可能比这更久的收件箱在开设时声明
# ``processing_timeout``，租约按它放长（:func:`_lease_ms`）。
PROCESSING_RETRY = RetryPolicy(
    n=4,
    backoff="exponential",
    base_delay_ms=30_000,
    max_delay_ms=600_000,
    lease_ms=900_000,
)

# 另一个进程正拿着这条消息时，等它的租约到期后再多等这么久才重新看。
_LEASE_MARGIN_MS = 1_000

# 声明了处理时限的收件箱：租约比时限多出这么久。超时取消之后还要把这次失败记下来、
# 排好重试，这段时间里占位仍然归这个进程，别的进程不该接管。
LEASE_OVER_TIMEOUT_MS = 60_000

# 消费通道一次最多拿几条没确认的消息（:meth:`app.infra.rabbitmq.MQ.open_channel` 的默认）。
_PREFETCH = 10

# 停下时最多等正在处理的消息多久（秒）。要比部署平台给进程的退出宽限（K8s 默认 30 秒）短，
# 否则等到一半进程就被杀掉，后面放回和放开占位那两步都来不及做。
STOP_GRACE_SECONDS = 20.0

OnMessage = Callable[[Message], Awaitable[None]]
OnQuestion = Callable[[Message], Awaitable[str | None]]
OnOpen = Callable[[], Awaitable[None]]
OnFinalFailure = Callable[[Message, BaseException], Awaitable[None]]
ConsumeWhile = Callable[[], AbstractAsyncContextManager[None]]

# 声明了 consume_while 的收件箱：进了上下文之后开设失败，隔多久再来一次（秒）。
OPEN_RETRY_SECONDS = 30.0


@dataclass(frozen=True)
class InboxSpec:
    name: str
    on_message: OnMessage
    on_question: OnQuestion | None
    processing_timeout: timedelta | None = None
    one_at_a_time: bool = False
    on_open: OnOpen | None = None
    on_final_failure: OnFinalFailure | None = None
    consume_while: ConsumeWhile | None = None


INBOX_REGISTRY: dict[str, InboxSpec] = {}


def inbox(
    name: str,
    *,
    on_message: OnMessage,
    on_question: OnQuestion | None = None,
    processing_timeout: timedelta | None = None,
    one_at_a_time: bool = False,
    on_open: OnOpen | None = None,
    on_final_failure: OnFinalFailure | None = None,
    consume_while: ConsumeWhile | None = None,
) -> None:
    """声明本 App 拥有名为 ``name`` 的收件箱。在 App 的接线模块里调，进程启动时开设。

    ``on_message`` 处理普通消息和 ``not_delivered`` 告知，抛异常即处理失败（会重试）。
    ``on_question`` 回答问题，返回回答正文；返回 ``None`` 表示没有回答。不给它的
    收件箱不接受提问，问它的一律拿到"没有回答"。

    ``processing_timeout`` / ``one_at_a_time`` / ``on_open`` / ``on_final_failure`` /
    ``consume_while`` 见模块说明。
    """
    participant(name)
    if name in INBOX_REGISTRY:
        raise RuntimeError(f"inbox {name!r} is already declared in this process")
    if processing_timeout is not None and processing_timeout <= timedelta(0):
        raise ValueError("processing_timeout must be positive")
    INBOX_REGISTRY[name] = InboxSpec(
        name,
        on_message,
        on_question,
        processing_timeout=processing_timeout,
        one_at_a_time=one_at_a_time,
        on_open=on_open,
        on_final_failure=on_final_failure,
        consume_while=consume_while,
    )


def _lease_ms(spec: InboxSpec) -> int:
    """这个收件箱的消息占位多久。声明了处理时限的，放长到时限之上。"""
    if spec.processing_timeout is None:
        return PROCESSING_RETRY.lease_ms
    return int(spec.processing_timeout.total_seconds() * 1000) + LEASE_OVER_TIMEOUT_MS


def clear_inboxes() -> None:
    INBOX_REGISTRY.clear()


# (channel, queue, consumer_tag)，停的时候逐个取消。
_consumers: list[tuple[Any, Any, str]] = []

# 正在处理消息的那些任务（aio-pika 每送来一条就起一个任务跑处理函数）。停的时候等它们。
_in_flight: set[asyncio.Task] = set()


def _tracked(handler):
    async def run(incoming: AbstractIncomingMessage) -> None:
        task = asyncio.current_task()
        _in_flight.add(task)
        try:
            await handler(incoming)
        finally:
            _in_flight.discard(task)

    return run


async def _consume(route, handler, *, prefetch_count: int = _PREFETCH) -> None:
    channel = await mq.open_channel(prefetch_count=prefetch_count)
    queue = await channel.get_queue(lane_queue(route.queue, lane()))
    tag = await queue.consume(_tracked(handler))
    _consumers.append((channel, queue, tag))
    logger.info("messaging: consuming %s", queue.name)


@dataclass
class _HeldOpener:
    """一个声明了 consume_while 的收件箱在后台的那一路：等着进上下文，或者已经在消费。"""

    task: asyncio.Task | None = None
    consuming: bool = False


_held_openers: list[_HeldOpener] = []
# 停止时设上：已经在消费的那几路据此退出上下文。每次开设换一个新的。
_let_go: asyncio.Event | None = None


async def _open(spec: InboxSpec, route) -> None:
    if spec.on_open is not None:
        await spec.on_open()
    await _consume(
        route,
        _inbox_handler(spec),
        prefetch_count=1 if spec.one_at_a_time else _PREFETCH,
    )


async def _open_while_held(
    spec: InboxSpec, route, opener: _HeldOpener, let_go: asyncio.Event
) -> None:
    while True:
        try:
            async with spec.consume_while():
                await _open(spec, route)
                opener.consuming = True
                await let_go.wait()
                return
        except Exception:
            logger.exception(
                "messaging: opening %s failed while held; trying again in %.0fs",
                spec.name,
                OPEN_RETRY_SECONDS,
            )
        await asyncio.sleep(OPEN_RETRY_SECONDS)


async def start_receiving() -> None:
    """开设本进程声明的全部收件箱并开始消费；同时消费本泳道的定时队列。

    声明了 ``consume_while`` 的收件箱在后台等到持有之后才开设、消费，这里不等它。
    """
    global _let_go
    _let_go = asyncio.Event()
    await mq.declare_route(SCHEDULED, lane=lane())
    await _consume(SCHEDULED, _on_scheduled)
    for spec in INBOX_REGISTRY.values():
        route = inbox_route(spec.name)
        await mq.declare_route(route, lane=lane())
        if spec.consume_while is None:
            await _open(spec, route)
            continue
        opener = _HeldOpener()
        opener.task = asyncio.create_task(
            _open_while_held(spec, route, opener, _let_go),
            name=f"messaging-open-{spec.name}",
        )
        _held_openers.append(opener)


async def stop_receiving() -> None:
    """停止消费：取消消费者，等正在处理的消息，等不完的放回去（见模块说明）。"""
    openers, _held_openers[:] = list(_held_openers), []
    # 还没开始消费的那几路（在等着进上下文，或者正在开设）：直接放弃，不让它们在下面取快照
    # 之后才开始消费。
    waiting = [o.task for o in openers if not o.consuming]
    for task in waiting:
        task.cancel()
    await asyncio.gather(*waiting, return_exceptions=True)

    consumers, _consumers[:] = list(_consumers), []
    for _channel, queue, tag in consumers:
        try:
            await queue.cancel(tag)
        except Exception:
            logger.warning("messaging: cancel %s failed", queue.name, exc_info=True)

    unfinished: set[asyncio.Task] = set()
    running = {t for t in _in_flight if not t.done()}
    if running:
        _, unfinished = await asyncio.wait(running, timeout=STOP_GRACE_SECONDS)

    # 先关通道再取消：通道关着，被取消的那几条不会被拒收进死信，broker 把它们放回原队列。
    for channel, _queue, _tag in consumers:
        if not channel.is_closed:
            try:
                await channel.close()
            except Exception:
                logger.warning("messaging: close channel failed", exc_info=True)

    if unfinished:
        logger.warning(
            "messaging: %d message(s) still being handled after %.0fs; cancelled and "
            "left for the next process",
            len(unfinished),
            STOP_GRACE_SECONDS,
        )
        # 关通道时 aio-pika 自己会取消挂在那条通道上的处理任务；再取消一次会打断它们放开
        # 占位的那一步，所以只取消还没被取消的。
        for task in unfinished:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.wait(unfinished, timeout=STOP_GRACE_SECONDS)

    # 消费停了、正在处理的都处理完或放回去了，这时才退出那几路的上下文（放开锁之类）。
    if _let_go is not None:
        _let_go.set()
    held = [o.task for o in openers if o.consuming]
    await asyncio.gather(*held, return_exceptions=True)


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
    lease_ms: int | None = None,
    on_final_failure: OnFinalFailure | None = None,
) -> None:
    """按消息 id 去重后跑 ``run``；普通消息失败按 :data:`PROCESSING_RETRY` 重投，用完就抛。

    ``lease_ms`` 是占位的租约，不给就是 :data:`PROCESSING_RETRY` 的。``on_final_failure``
    在重试用完、抛出去进死信之前调一次（见模块说明）。

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
        lease_ms=lease_ms if lease_ms is not None else PROCESSING_RETRY.lease_ms,
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
    except asyncio.CancelledError:
        # 进程在停（:func:`stop_receiving`）：放开占位，重投的那一份马上有人接。不重投、
        # 不进死信——通道已经关了，broker 会把这条放回原队列。
        try:
            await mark_failed(
                edge_id=edge_id,
                idempotent_key=message.message_id,
                last_error="cancelled while the process was stopping",
                worker_id=claim_token,
            )
        except Exception:
            logger.warning(
                "messaging: could not release %s %s after cancelling it",
                edge_id,
                message.message_id,
                exc_info=True,
            )
        raise
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
            if on_final_failure is not None:
                try:
                    await on_final_failure(message, exc)
                except Exception:
                    logger.exception(
                        "messaging: the final-failure hook of %s failed for %s",
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
        elif spec.processing_timeout is None:
            await spec.on_message(message)
        else:
            # 超时抛 TimeoutError，跟处理函数自己抛异常一样按处理失败重试。
            async with asyncio.timeout(spec.processing_timeout.total_seconds()):
                await spec.on_message(message)

    async def process(incoming: AbstractIncomingMessage) -> None:
        received = dict(incoming.headers or {})
        message = Message.from_json(json.loads(incoming.body))
        async with bind_context(extract_context(received)):
            await _run_once(
                message,
                received,
                route=route,
                edge_id=edge_id,
                run=run,
                lease_ms=_lease_ms(spec),
                on_final_failure=spec.on_final_failure,
            )

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
