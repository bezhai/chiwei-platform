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

**进死信的原因只有一个：业务处理函数本身失败，有限次重试已经用完，拥有者也没要求不限次数
重试。** 普通消息的处理函数抛异常 → 按 :data:`PROCESSING_RETRY` 延时重投；次数用完 → 拒收，
broker 把它送进本泳道的 ``isolated_dead_letters_<泳道>``，原样保留消息体和消息头，可以查看、
重放回原收件箱（:mod:`app.messaging.dead_letters`，入口是 ``/admin/messaging/dead-letters*``）。
（消息本身解不开的也进死信：处理函数根本没法跑。）领取、标记成功或失败、租约冲突后的重投、
重投那一份的发布、定时转交、分段发布失败，都是基础设施失败，不是消息的问题：普通消息停一会儿
放回原队列（停的时长翻倍、封顶 :data:`PUT_BACK_CAP_SECONDS`，不限次数），问题确认掉。结论
只在 :func:`_settle` 一处交给 broker，见下面"一条消息的处理"那一节。

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
* ``retry_without_limit`` —— 拥有者对一条处理失败的普通消息的判断：交回一个时长，表示这条
  不限次数重试、永不进死信，退避按指数翻倍、封顶在这个时长；交回 ``None``，照常有限次重试后
  进死信。不限次数重试的每一次失败都让人看得到：记录者里记一行 ``retrying``（错误和下一次的
  延时），日志里一条 warning。判断本身出错按"不限次数"算，封顶用 :data:`PROCESSING_RETRY`
  的上限——宁可多试，不能把拥有者要保住的那条送进死信。
* ``consume_while`` —— 只在持有它（一个异步上下文，比如一把跨进程的独占锁）期间消费。
  开设时不在启动流程里等它：队列照常建好，启动照常返回，后台等到进了这个上下文，才跑
  ``on_open``、开始消费；停止时等正在处理的消息处理完（见下面"停下时"）之后才退出这个
  上下文。进了上下文之后 ``on_open`` 失败，就退出上下文、隔 :data:`OPEN_RETRY_SECONDS`
  再来一次——这时启动早已返回，失败不能再靠让启动失败来暴露。

**停下时正在处理的消息**（:func:`stop_receiving`）：先取消消费者（不再有新消息进来），等正在
处理的那几条处理完、照常确认，最多等 :data:`STOP_GRACE_SECONDS`。等不完的，先关通道再取消：
通道关着，取消时不会拒收进死信，broker 把没确认的消息放回原队列；取消的同时放开它的去重
占位，重投的那一份马上有人接，不用等租约过期。**问题例外**：问题不重试，等不完的问题在通道
还开着的时候取消，确认掉、占位收成"处理过"，不退回队列，下一个进程不会再答它一遍。声明了
``consume_while`` 的收件箱：还在等着进上下文的，停止时直接放弃等待；已经在消费的，等上面这些
都做完才退出上下文。

**问题不重试，至多答一次。** 问题的处理函数抛异常、返回空、或者收件箱不接受提问，都立刻给
提问方回一个"没有回答"。问题这条路径上的任何失败——回复发不出去、去重状态读写失败、消息
本身解不开——都只记一笔日志、确认掉：不重投，不进死信。领到一个问题先把占位收成"处理过"再
答（:func:`_answer_question`），所以确认之后再来的同 id 副本也不会被答第二遍。提问方已经不等了
（过了它带来的截止时刻）的问题直接跳过。

**定时送达。** 定时队列里的消息到点才被送到这里：先看时刻到了没有——没到（延时被
broker 上限截成了几段）就按剩下的时长再排一段；到了，就在这一刻判断对方开设了收件箱
没有，投递或者记 ``not_delivered`` 并给原发送方发一条 ``not_delivered`` 告知。告知是原
发送方自己的消息被退回，所以发送方和接收方都是原发送方。告知的 id 由原消息 id 推出来，
这一步失败重试时再发的告知还是同一个 id，发送方按 id 去重。定时通道上的一切失败（分段、
转交、写记录、发告知）都是基础设施失败，一直放回重试，不进死信。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
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
from app.messaging.record import Outcome, record
from app.messaging.sending import (
    ANSWER_BY_HEADER,
    REPLY_RK_HEADER,
    deliver,
    publish_recorded,
)
from app.runtime.inflight import claim_inflight, mark_failed, mark_succeeded
from app.runtime.propagation import bind_context, extract_context
from app.runtime.retry import DELIVERY_COUNT_HEADER, decide_retry, delivery_count
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
RetryWithoutLimit = Callable[[Message], Awaitable[timedelta | None]]

# 基础设施失败时，停多久把原消息放回原队列（秒）：第一次停这么久，之后每次翻倍，封顶
# PUT_BACK_CAP_SECONDS。不用立即重投的 nack，避免 broker 和这里空转。
PUT_BACK_BASE_SECONDS = 1.0
PUT_BACK_CAP_SECONDS = 60.0
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
    consume_while: ConsumeWhile | None = None
    retry_without_limit: RetryWithoutLimit | None = None


INBOX_REGISTRY: dict[str, InboxSpec] = {}


def inbox(
    name: str,
    *,
    on_message: OnMessage,
    on_question: OnQuestion | None = None,
    processing_timeout: timedelta | None = None,
    one_at_a_time: bool = False,
    on_open: OnOpen | None = None,
    consume_while: ConsumeWhile | None = None,
    retry_without_limit: RetryWithoutLimit | None = None,
) -> None:
    """声明本 App 拥有名为 ``name`` 的收件箱。在 App 的接线模块里调，进程启动时开设。

    ``on_message`` 处理普通消息和 ``not_delivered`` 告知，抛异常即处理失败（会重试）。
    ``on_question`` 回答问题，返回回答正文；返回 ``None`` 表示没有回答。不给它的
    收件箱不接受提问，问它的一律拿到"没有回答"。

    ``processing_timeout`` / ``one_at_a_time`` / ``on_open`` / ``consume_while`` /
    ``retry_without_limit`` 见模块说明。
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
        consume_while=consume_while,
        retry_without_limit=retry_without_limit,
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
# 其中正在回答问题的那些。停的时候它们要在通道还开着时取消、确认掉（问题不重试）。
_answering: set[asyncio.Task] = set()


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
    # 停在"放回之前等一会儿"的那几条不用等：下面关了通道，它们自然回到原队列。
    running = {t for t in _in_flight if not t.done() and t not in _putting_back}
    if running:
        _, unfinished = await asyncio.wait(running, timeout=STOP_GRACE_SECONDS)

    # 普通消息先关通道再取消：通道关着，被取消的那几条不会被拒收进死信，broker 把它们放回
    # 原队列。问题不一样，在关通道之前处理（见下）。
    questions = {t for t in unfinished if t in _answering}
    if questions:
        logger.warning(
            "messaging: %d question(s) still being answered after %.0fs; dropped without "
            "an answer, never redelivered",
            len(questions),
            STOP_GRACE_SECONDS,
        )
        for task in questions:
            task.cancel()
        await asyncio.wait(questions, timeout=STOP_GRACE_SECONDS)
        unfinished = unfinished - questions

    for channel, _queue, _tag in consumers:
        if not channel.is_closed:
            try:
                await channel.close()
            except Exception:
                logger.warning("messaging: close channel failed", exc_info=True)
    unfinished |= {t for t in _putting_back if not t.done()}

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
# 一条消息的处理：业务处理和记账分开，最外层统一决定确认、放回还是进死信
# ---------------------------------------------------------------------------
#
# 进死信的原因只有一个：**业务处理函数（收件箱的 on_message）本身失败，有限次重试已经用完，
# 拥有者也没要求不限次数重试**（:func:`_after_owner_failure`）。消息解不开也进死信——那是它
# 本身坏了，处理函数根本没法跑。除此之外，领取、标记成功或失败、租约冲突后的重投、重投那一份
# 的发布、定时转交、分段发布，这些环节抛出来的都是基础设施失败：由 :func:`_consumer` 统一接住，
# 普通消息放回原队列（:data:`Verdict.PUT_BACK`，停一会儿再放，停的时长按次数翻倍、有上限，
# 不限次数），问题确认掉（问题不重试）。结论只在 :func:`_settle` 一处交给 broker；
# ``reject(requeue=False)`` 在整个模块里只出现在那里。


class Verdict(Enum):
    """一条消息处理到最后交给 broker 的结论。"""

    # 处理完了：成功、跳过、问题答完或者没答成、重投那一份已经排出去。
    ACK = "ack"
    # 基础设施失败：停一会儿，原样放回原队列。不进死信，不限次数。
    PUT_BACK = "put_back"
    # 业务处理失败到头（或者消息本身解不开）：进本泳道的死信，人工查看、重放。
    DEAD_LETTER = "dead_letter"


Decide = Callable[[AbstractIncomingMessage], Awaitable[Verdict]]

# 按消息体数的连续放回次数，决定下一次停多久；确认或进死信时清掉。
_put_backs: dict[str, int] = {}
# 正停在"放回之前等一会儿"的任务。停止消费时不等它们，直接取消（通道关了，消息自然回队列）。
_putting_back: set[asyncio.Task] = set()


def _put_back_delay(key: str) -> float:
    count = _put_backs.get(key, 0) + 1
    _put_backs[key] = count
    return min(PUT_BACK_BASE_SECONDS * 2 ** (count - 1), PUT_BACK_CAP_SECONDS)


async def _settle(incoming: AbstractIncomingMessage, verdict: Verdict) -> None:
    """把结论交给 broker。**进死信只有这里的 DEAD_LETTER 一条路。**"""
    key = hashlib.sha256(incoming.body).hexdigest()
    if verdict is Verdict.ACK:
        _put_backs.pop(key, None)
        await incoming.ack()
        return
    if verdict is Verdict.PUT_BACK:
        delay = _put_back_delay(key)
        task = asyncio.current_task()
        _putting_back.add(task)
        try:
            await asyncio.sleep(delay)
        finally:
            _putting_back.discard(task)
        await incoming.reject(requeue=True)
        return
    _put_backs.pop(key, None)
    await incoming.reject(requeue=False)


def _consumer(on_message: Decide, on_question: Decide | None = None):
    """一条队列的消费回调：业务处理和记账交给 ``on_message`` / ``on_question``，这里接住它们
    抛出来的基础设施失败，再把结论交给 :func:`_settle`。"""

    async def handler(incoming: AbstractIncomingMessage) -> None:
        is_question = on_question is not None and _is_question(incoming.body)
        task = asyncio.current_task()
        if is_question:
            _answering.add(task)
        try:
            try:
                verdict = await (on_question if is_question else on_message)(incoming)
            except asyncio.CancelledError:
                # 进程在停（:func:`stop_receiving`）。问题：确认掉，不退回队列。普通消息：通道
                # 已经关了，broker 把它放回原队列。
                if is_question and not incoming.channel.is_closed:
                    await incoming.ack()
                raise
            except Exception:
                logger.warning(
                    "messaging: bookkeeping or hand-over for a delivery on %s failed; %s",
                    incoming.routing_key,
                    "acknowledged, a question is never handled twice"
                    if is_question
                    else "putting it back",
                    exc_info=True,
                )
                verdict = Verdict.ACK if is_question else Verdict.PUT_BACK
            await _settle(incoming, verdict)
        finally:
            _answering.discard(task)

    return handler


def _decode(incoming: AbstractIncomingMessage) -> Message | None:
    try:
        return Message.from_json(json.loads(incoming.body))
    except Exception:
        logger.exception("messaging: undecodable delivery on %s", incoming.routing_key)
        return None


async def _handle(
    message: Message,
    received: dict[str, Any],
    *,
    route,
    edge_id: str,
    lease_ms: int,
    run: Callable[[], Awaitable[None]],
    after_failure: Callable[[Message, dict[str, Any], Any, str, Exception], Awaitable[Verdict]],
) -> Verdict:
    """领取 → 业务处理 → 标记。只有 ``run`` 的失败算业务失败，交给 ``after_failure``；别的步骤
    抛出去，由 :func:`_consumer` 按基础设施失败处理。

    按消息 id 去重：处理成功过的不再处理；另一个进程正拿着（租约没过期）的，按剩下的租约延时
    重新排回去——那个进程要是半路死了，租约过期后由这里接管。每次占位用一个只属于这一次的
    标记：租约过期被别人接管之后，这里的成功或失败都不再改那一行，也不再重投。
    """
    claim_token = f"{WORKER_ID}#{uuid.uuid4().hex[:12]}"
    claim = await claim_inflight(
        edge_id=edge_id,
        idempotent_key=message.message_id,
        data_table=route.queue,
        worker_id=claim_token,
        lease_ms=lease_ms,
        trace_id=extract_context(received).trace_id,
    )
    if claim.action == "skip":
        if claim.locked_until is not None:
            wait_ms = hop_delay_ms(claim.locked_until, datetime.now(UTC))
            await publish(
                route, message.to_json(), headers=received, delay_ms=wait_ms + _LEASE_MARGIN_MS
            )
            logger.info(
                "messaging: %s %s is held by another worker; re-queued",
                edge_id,
                message.message_id,
            )
        return Verdict.ACK

    try:
        await run()
    except asyncio.CancelledError:
        await _release(edge_id, message, claim_token)
        raise
    except Exception as exc:
        still_mine = await mark_failed(
            edge_id=edge_id,
            idempotent_key=message.message_id,
            last_error=f"{type(exc).__name__}: {exc}",
            worker_id=claim_token,
        )
        if not still_mine:
            logger.warning(
                "messaging: %s %s failed after its claim was taken over; the new holder "
                "owns the outcome",
                edge_id,
                message.message_id,
            )
            return Verdict.ACK
        return await after_failure(message, received, route, edge_id, exc)

    if not await mark_succeeded(
        edge_id=edge_id, idempotent_key=message.message_id, worker_id=claim_token
    ):
        logger.warning(
            "messaging: %s %s finished after its claim was taken over",
            edge_id,
            message.message_id,
        )
    return Verdict.ACK


async def _release(edge_id: str, message: Message, claim_token: str) -> None:
    """进程在停，这条被取消了：放开占位，重投的那一份马上有人接。放不开只记一笔。"""
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


async def _after_owner_failure(
    spec: InboxSpec,
    message: Message,
    received: dict[str, Any],
    route,
    edge_id: str,
    exc: Exception,
) -> Verdict:
    """拥有者的处理函数失败了一次：点名不限次数的按封顶退避再排；否则有限次重试，用完进死信。"""
    cap = await _unlimited_cap(spec, message, edge_id)
    if cap is not None:
        attempt = delivery_count(received) + 1
        delay_ms = RetryPolicy(
            n=attempt + 1,
            backoff="exponential",
            base_delay_ms=PROCESSING_RETRY.base_delay_ms,
            max_delay_ms=max(1, int(cap.total_seconds() * 1000)),
            lease_ms=PROCESSING_RETRY.lease_ms,
        ).delay_for_attempt(attempt)
        reason = (
            f"第 {attempt} 次处理失败（{type(exc).__name__}: {exc}），"
            f"{delay_ms / 1000:g} 秒后再试"
        )
        logger.warning(
            "messaging: %s %s failed; retrying without limit: %s",
            edge_id,
            message.message_id,
            reason,
        )
        await _note_retrying(message, reason)
        await publish(
            route,
            message.to_json(),
            headers={**received, DELIVERY_COUNT_HEADER: attempt},
            delay_ms=delay_ms,
        )
        return Verdict.ACK

    decision = decide_retry(headers=received, policy=PROCESSING_RETRY)
    if decision.action == "retry":
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
        return Verdict.ACK
    logger.error(
        "messaging: %s %s failed for good (%r); dead-lettered",
        edge_id,
        message.message_id,
        exc,
    )
    return Verdict.DEAD_LETTER


async def _unlimited_cap(spec: InboxSpec, message: Message, edge_id: str) -> timedelta | None:
    """拥有者对这条的判断。判断本身出错按"不限次数"算——宁可多试，不能把它要保住的那条送进死信。"""
    if spec.retry_without_limit is None:
        return None
    try:
        return await spec.retry_without_limit(message)
    except Exception:
        logger.exception(
            "messaging: the retry judgement of %s failed for %s; retrying without limit",
            edge_id,
            message.message_id,
        )
        return timedelta(milliseconds=PROCESSING_RETRY.max_delay_ms)


async def _note_retrying(message: Message, reason: str) -> None:
    """在记录者里记一行 ``retrying``。这一行只是把已经定了的重试记下来给人看：写不进去只记
    日志，不改这条消息的去向——为了它把消息放回去，等于记录者坏着的时候让处理函数空跑。"""
    try:
        await record(message, Outcome.RETRYING, reason=reason)
    except SendFailed:
        logger.exception("messaging: could not record the failure of %s", message.message_id)


def _edge(base: str) -> str:
    """去重状态的 edge：带上部署泳道。ppe 和 prod 共用 runtime_inflight 所在的库。"""
    return f"{base}@{lane_label()}"


def _is_question(body: bytes) -> bool:
    """只看 ``kind``，解不开就不是问题。用来在完整解码之前决定走哪条路。"""
    try:
        return json.loads(body).get("kind") == str(Kind.QUESTION)
    except Exception:
        return False


async def _run_owner(spec: InboxSpec, message: Message) -> None:
    if spec.processing_timeout is None:
        await spec.on_message(message)
        return
    # 超时抛 TimeoutError，跟处理函数自己抛异常一样按处理失败重试。
    async with asyncio.timeout(spec.processing_timeout.total_seconds()):
        await spec.on_message(message)


async def _deliver_to_owner(
    spec: InboxSpec, route, edge_id: str, incoming: AbstractIncomingMessage
) -> Verdict:
    message = _decode(incoming)
    if message is None:
        return Verdict.DEAD_LETTER
    received = dict(incoming.headers or {})
    async with bind_context(extract_context(received)):
        return await _handle(
            message,
            received,
            route=route,
            edge_id=edge_id,
            lease_ms=_lease_ms(spec),
            run=lambda: _run_owner(spec, message),
            after_failure=lambda *args: _after_owner_failure(spec, *args),
        )


async def _answer_question(
    spec: InboxSpec, route, edge_id: str, incoming: AbstractIncomingMessage
) -> Verdict:
    """一个问题：至多答一次。

    **先记为已处理，再答。** 领到之后立刻把占位收成"处理过"，然后才调回答函数。这样"回答已经
    执行"和"记为已处理"之间没有缝：不管回答时失败、进程停下被取消，还是确认之后又来了同 id
    的副本，这个问题都不会被再领一次。代价是记不成的时候（抛出去，最外层确认掉）这一次就不答
    ——问题本来就不重试，提问方拿到"没有回答"。
    """
    message = _decode(incoming)
    if message is None:
        return Verdict.ACK
    received = dict(incoming.headers or {})
    async with bind_context(extract_context(received)):
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
            return Verdict.ACK
        if await mark_succeeded(
            edge_id=edge_id, idempotent_key=message.message_id, worker_id=claim_token
        ):
            await _answer(spec, message, received)
    return Verdict.ACK


def _inbox_handler(spec: InboxSpec):
    route = inbox_route(spec.name)
    edge_id = _edge(f"inbox:{spec.name}")
    return _consumer(
        lambda incoming: _deliver_to_owner(spec, route, edge_id, incoming),
        lambda incoming: _answer_question(spec, route, edge_id, incoming),
    )


async def _answer(spec: InboxSpec, question: Message, received: dict[str, Any]) -> None:
    """回答一个问题，或者告诉提问方没有回答。回答函数失败、回答记不下来都在这里收住；最后那
    一次告知发不出去才会抛出去，由 :func:`_consumer` 确认掉——问题不重试。"""
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


async def _hand_over(incoming: AbstractIncomingMessage) -> Verdict:
    """定时队列上的一条：没到时刻就再排一段；到了就在这一刻转交给接收方的收件箱。

    这里没有业务处理函数：分段、转交、写记录、发"没有送达"告知，失败都是基础设施失败，一直
    放回重试，不进死信（:func:`_hand_over_again`）。"再排一段"不经过去重：同一条消息分段时
    每一段都会到这里一次，它们不是重复。
    """
    message = _decode(incoming)
    if message is None:
        return Verdict.DEAD_LETTER
    received = dict(incoming.headers or {})
    now = datetime.now(UTC)
    if message.time > now:
        await publish(
            SCHEDULED, message.to_json(), headers=received, delay_ms=hop_delay_ms(message.time, now)
        )
        return Verdict.ACK
    async with bind_context(extract_context(received)):
        return await _handle(
            message,
            received,
            route=SCHEDULED,
            edge_id=_edge("messaging:scheduled"),
            lease_ms=PROCESSING_RETRY.lease_ms,
            run=lambda: _deliver_due(message),
            after_failure=_hand_over_again,
        )


async def _hand_over_again(
    message: Message, received: dict[str, Any], route, edge_id: str, exc: Exception
) -> Verdict:
    logger.warning(
        "messaging: handing over %s failed (%r); putting it back",
        message.message_id,
        exc,
    )
    return Verdict.PUT_BACK


_on_scheduled = _consumer(_hand_over)


def _notice_id(message: Message) -> str:
    """一条定时消息的"没有送达"告知的 id：由原消息 id 确定地推出来，重试时不变。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"messaging:not_delivered:{message.message_id}").hex


async def _deliver_due(message: Message) -> None:
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
