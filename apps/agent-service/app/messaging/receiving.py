"""接收一侧：拥有者开设收件箱、处理送来的消息和问题；以及定时送达到点时的那一步。

**收件箱由拥有者开设。** 一个 App 在自己的接线模块里调 :func:`inbox` 声明它拥有的
收件箱和处理函数；进程启动时 :func:`start_receiving` 在本泳道建队列、开始消费。一个收件箱
是两条队列：收件箱本身装普通消息和退回的告知，旁边一条问题队列装问它的问题（布局见
:mod:`app.messaging.broker`），两条一起建、各自消费。发送方永远不会建它们，所以"这个名字
开设过收件箱没有"就是"这条队列在不在"。队列不设过期，拥有者不在线时消息一直留着。

**至少一次，按消息 id 去重。** broker 在消费者断开时会把没确认的消息重投，所以同一条
消息可能到两次。每个收件箱按 ``(inbox:<名字>@<泳道>, 消息 id)`` 在 ``runtime_inflight``
上占位（带泳道是因为 ppe 和 prod 共用一个库，同一个 id 在两条泳道各是各的一条）：处理成功过的不再处理；另一个进程正拿着它（租约还没过期）的，不丢，按剩下的
租约延时重新排回收件箱——那个进程要是半路死了，租约过期后由这里接管。接收方的处理
函数仍然可能在极端情况下看到同一条消息两次（处理完、还没来得及标记成功就崩了），
所以它拿到的 ``Message`` 带着 ``message_id``，自己据此去重。拥有者自己记的"处理过哪些"什么时候
可以不记，问 :func:`succeeded_message_ids`：成功记下来了，同一条就再也到不了处理函数。

**进死信的原因只有一个：业务处理函数本身失败，有限次重试已经用完，拥有者也没要求不限次数
重试。** 普通消息的处理函数抛异常 → 按 :data:`PROCESSING_RETRY` 延时重投；次数用完 → 拒收，
broker 把它送进本泳道的 ``isolated_dead_letters_<泳道>``，原样保留消息体和消息头，可以查看、
重放回原收件箱（:mod:`app.messaging.dead_letters`，入口是 ``/admin/messaging/dead-letters*``）。
（消息本身解不开的也进死信：处理函数根本没法跑。）领取、标记成功或失败、租约冲突后的重投、
重投那一份的发布、定时转交、分段发布失败，都是基础设施失败，不是消息的问题：普通消息停一会儿
放回原队列（停的时长翻倍、封顶 :data:`PUT_BACK_CAP_SECONDS`，不限次数），问题确认掉。结论
只在 :func:`_settle` 一处交给 broker，见下面"一条消息的处理"那一节。

**拥有者开设时可以多声明几件事**（:func:`inbox`），都只管普通消息，问题不受它们影响（见下面
"问题不排在普通消息后面"）：

* ``processing_timeout`` —— 一条消息最多处理多久。超过就取消，算一次处理失败（按上面的
  重试、死信处理）；这条消息的去重占位租约相应放长到它之上
  （:data:`LEASE_OVER_TIMEOUT_MS`），所以处理还没完时到的重复副本不会把它当成"前一个
  进程死了"接管过去。不声明就是默认的 15 分钟租约，处理本身只受下面一次投递的期限约束。
  声明了的，要给领取和记结果留出 :data:`CLAIM_AND_SETTLE_ROOM`，放不进期限就拒绝开设。
* ``on_open`` —— 收件箱开设（队列建好）之后、开始消费普通消息之前调一次。拥有者在这里按
  自己的状态做启动时该做的事，可以往自己的收件箱里发消息，它们等这一步结束才被处理。它抛
  异常，启动就失败。
* ``retry_without_limit`` —— 拥有者对一条处理失败的普通消息的判断：交回一个时长，表示这条
  不限次数重试、永不进死信，退避按指数翻倍、封顶在这个时长；交回 ``None``，照常有限次重试后
  进死信。不限次数重试的每一次失败都让人看得到：记录者里记一行 ``retrying``（错误和下一次的
  延时），日志里一条 warning。判断本身出错按"不限次数"算，封顶用 :data:`PROCESSING_RETRY`
  的上限——宁可多试，不能把拥有者要保住的那条送进死信。
* ``consume_while`` —— 只在持有它（一个异步上下文，比如一把跨进程的独占锁）期间消费普通消息。
  开设时不在启动流程里等它：队列照常建好，启动照常返回，后台等到进了这个上下文，才跑
  ``on_open``、开始消费；停止时等正在处理的消息处理完（见下面"停下时"）之后才退出这个
  上下文。进了上下文之后 ``on_open`` 失败，就退出上下文、隔 :data:`OPEN_RETRY_SECONDS`
  再来一次——这时启动早已返回，失败不能再靠让启动失败来暴露。

**名字到进程启动时才知道的收件箱**（:func:`inboxes_at_start`）。:func:`inbox` 在接线模块
import 时就要名字，可有的名字存在库里（三姐妹的名字取自人设表），import 的时候库还没准备好。
这类拥有者在接线里只声明一个"开设它们"的函数，:func:`start_receiving` 在开设任何收件箱之前
调它一次，它在里面取名字、对每个名字调 :func:`inbox`。它抛异常，启动就失败，一个收件箱都
不开：名字就是地址，名字有问题时不该带着其中一部分收件箱运行。失败的那一次里已经按名字声明
的收件箱一并撤掉，声明本身留着：同一个进程再开始接收时整组重新调一遍，名字还有问题就照样失败，
问题没了就开出完整的一组——不会把上一次开了一半的那几个当成开好的。全部调成功之后，每个声明
在一个进程里就不再调，停了再开始接收时开设的还是那一次取到的那几个名字。

**问题不排在普通消息后面。** 问题走自己的队列、自己的消费通道（prefetch :data:`_PREFETCH`），
队列一建好就开始消费：不等 ``on_open``，也不等 ``consume_while``。
所以一个收件箱正在处理一条要跑很久的消息、后面还排着几条时，问它的问题照样在提问方的截止
时刻之前答上。不让 ``consume_while`` 管问题，是因为它护着的是普通消息的处理函数要独占的东西
（比如 world 的卷只能有一个写的进程），回答用不着：没持有的进程——比如滚动发布时等着旧进程
放手的新进程——照样能答，提问方不用为了等锁白等到截止时刻。代价落在拥有者身上：``on_question``
可能跟 ``on_message`` 同时跑，也可能跑在没持有 ``consume_while`` 的进程里，它只该读，不该改
``on_message`` 要独占的东西。

**停下时正在处理的消息**（:func:`stop_receiving`）：先取消消费者（不再有新消息进来），等正在
处理的那几条处理完、照常确认，最多等 :data:`STOP_GRACE_SECONDS`。等不完的，先关通道再取消：
通道关着，取消时不会拒收进死信，broker 把没确认的消息放回原队列；取消的同时放开它的去重
占位，重投的那一份马上有人接，不用等租约过期。**问题例外**：问题不重试，等不完的问题在通道
还开着的时候取消，确认掉、占位收成"处理过"，不退回队列，下一个进程不会再答它一遍。声明了
``consume_while`` 的收件箱：还在等着进上下文的，停止时直接放弃等待；已经在消费的，等上面这些
都做完才退出上下文。

**一次投递有期限。** 从领取、处理到记下结果，整体最多 :data:`DELIVERY_DEADLINE`，比 broker 等
确认的时限短。领取和记结果那十几次查库在处理时限之外，库很慢时按各自的上限加起来能超过 broker 的
时限，broker 就会关掉整个通道，同一通道上别的投递跟着被取消。到了期限就取消这一次、放开占位，
只把这一条放回原队列（:data:`Verdict.PUT_BACK`），通道照常开着。

**被取消就放开占位。** 正在处理的投递被取消有三个来源：它到了期限（上一段），进程在停（再上
一段），和它所在的消费通道被关掉——broker 关的，或者连接断了。都一样：这里放开它的去重占位，
不管取消落在领取、处理、标记哪一步（:func:`_handle`），消息回到原队列。记下来的原因分得开
（:func:`_release`）；通道不是因为进程在停而关掉时，另记一条带着关闭原因的 warning
（:func:`_closed_while_consuming`）。

**问题不重试，至多答一次。** 问题的处理函数抛异常、返回空、或者收件箱不接受提问（没给
``on_question`` 的收件箱也有问题队列），都立刻给提问方回一个"没有回答"。问题这条路径上的任何失败——回复发不出去、去重状态读写失败、消息
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
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from aio_pika.abc import AbstractIncomingMessage
from aio_pika.exceptions import ChannelInvalidStateError

from app.infra.rabbitmq import BROKER_ACK_TIMEOUT_MS, lane_queue, mq
from app.messaging.broker import (
    SCHEDULED,
    headers,
    hop_delay_ms,
    inbox_route,
    lane,
    lane_label,
    publish,
    question_route,
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
from app.runtime.inflight import (
    claim_inflight,
    mark_failed,
    mark_succeeded,
    succeeded_keys,
)
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

# 一次投递从领取、处理到记下结果，整体最多多久（:func:`_handle`）。比 broker 等确认的时限短
# DELIVERY_MARGIN：期限到了之后还有几件事要在 broker 动手之前做完——被取消的那一步收尾（最多
# :data:`app.data.dialect.TERMINATE_GRACE_SECONDS`，5 秒）、放开占位（RELEASE_SECONDS，它被截断时
# 再加一次收尾）、放回之前停的那一会儿（最多 PUT_BACK_CAP_SECONDS），加起来不到一分半。
DELIVERY_MARGIN = timedelta(minutes=2)
DELIVERY_DEADLINE = timedelta(milliseconds=BROKER_ACK_TIMEOUT_MS) - DELIVERY_MARGIN

# 声明了处理时限的收件箱，处理时限之外要给领取和记结果留出多久。库正常时这两步是毫秒级；库慢到
# 用完它，这一次就到了期限、交还重投。开设收件箱时检查处理时限加上它放得进期限。
CLAIM_AND_SETTLE_ROOM = timedelta(minutes=5)

# 被取消之后放开占位最多等多久（秒）。放开只是一条短的更新；等不到就只记一笔，占位留到租约过期。
RELEASE_SECONDS = 15.0

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

# 进程在停（:func:`stop_receiving` 开始了）。处理被取消、消费通道关掉时据此分清是进程在停，
# 还是通道被 broker 关掉、连接断了。
_stopping = False


@dataclass(frozen=True)
class InboxSpec:
    name: str
    on_message: OnMessage
    on_question: OnQuestion | None
    processing_timeout: timedelta | None = None
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
    on_open: OnOpen | None = None,
    consume_while: ConsumeWhile | None = None,
    retry_without_limit: RetryWithoutLimit | None = None,
) -> None:
    """声明本 App 拥有名为 ``name`` 的收件箱。在 App 的接线模块里调，进程启动时开设。

    ``on_message`` 处理普通消息和 ``not_delivered`` 告知，抛异常即处理失败（会重试）。
    ``on_question`` 回答问题，返回回答正文；返回 ``None`` 表示没有回答。不给它的
    收件箱不接受提问，问它的一律拿到"没有回答"。问题走自己的队列，``on_question`` 可能
    跟 ``on_message`` 同时跑、也可能跑在没持有 ``consume_while`` 的进程里，所以它只该读。

    ``processing_timeout`` / ``on_open`` / ``consume_while`` / ``retry_without_limit``
    见模块说明。
    """
    participant(name)
    if name in INBOX_REGISTRY:
        raise RuntimeError(f"inbox {name!r} is already declared in this process")
    if processing_timeout is not None and processing_timeout <= timedelta(0):
        raise ValueError("processing_timeout must be positive")
    if (
        processing_timeout is not None
        and processing_timeout + CLAIM_AND_SETTLE_ROOM > DELIVERY_DEADLINE
    ):
        raise ValueError(
            f"inbox {name!r}: processing_timeout {processing_timeout} leaves less than "
            f"{CLAIM_AND_SETTLE_ROOM} for claiming and settling inside the delivery deadline "
            f"{DELIVERY_DEADLINE}"
        )
    INBOX_REGISTRY[name] = InboxSpec(
        name,
        on_message,
        on_question,
        processing_timeout=processing_timeout,
        on_open=on_open,
        consume_while=consume_while,
        retry_without_limit=retry_without_limit,
    )


OpenAtStart = Callable[[], Awaitable[None]]

# 还没调成功过的"启动时再开"的声明（:func:`inboxes_at_start`）。开始接收时逐个调，全部成功才清空。
INBOXES_AT_START: list[OpenAtStart] = []


def inboxes_at_start(open_them: OpenAtStart) -> None:
    """声明一组名字到进程启动时才知道的收件箱。在 App 的接线模块里调，见模块说明。

    ``open_them`` 在 :func:`start_receiving` 开设任何收件箱之前调，在里面取名字、对每个
    名字调 :func:`inbox`。它抛异常，启动就失败，它这一次声明的收件箱撤掉，下一次开始接收时
    再调一遍。
    """
    INBOXES_AT_START.append(open_them)


def _lease_ms(spec: InboxSpec) -> int:
    """这个收件箱的消息占位多久。声明了处理时限的，放长到时限之上。"""
    if spec.processing_timeout is None:
        return PROCESSING_RETRY.lease_ms
    return int(spec.processing_timeout.total_seconds() * 1000) + LEASE_OVER_TIMEOUT_MS


def clear_inboxes() -> None:
    INBOX_REGISTRY.clear()
    INBOXES_AT_START.clear()


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


async def _consume(route, handler) -> None:
    channel = await mq.open_channel(prefetch_count=_PREFETCH)
    queue = await channel.get_queue(lane_queue(route.queue, lane()))
    channel.close_callbacks.add(_closed_while_consuming(queue.name))
    tag = await queue.consume(_tracked(handler))
    _consumers.append((channel, queue, tag))
    logger.info("messaging: consuming %s", queue.name)


def _closed_while_consuming(queue_name: str):
    """消费通道关了、又不是因为进程在停时，记下关闭的原因：broker 关通道时，原因只在这里看得到。
    通道重新打开之后接着消费，这条通道上正在处理的投递被取消、放开占位（:func:`_release`）。"""

    def closed(_channel, exc: BaseException | None) -> None:
        if not _stopping:
            logger.warning(
                "messaging: the channel consuming %s was closed (%r); deliveries being "
                "handled on it are cancelled and go back to the queue",
                queue_name,
                exc,
            )

    return closed


@dataclass
class _HeldOpener:
    """一个声明了 consume_while 的收件箱在后台的那一路：等着进上下文，或者已经在消费。"""

    task: asyncio.Task | None = None
    consuming: bool = False


_held_openers: list[_HeldOpener] = []
# 停止时设上：已经在消费的那几路据此退出上下文。每次开设换一个新的。
_let_go: asyncio.Event | None = None


async def _open(spec: InboxSpec) -> None:
    """跑 ``on_open``，然后开始消费普通消息。"""
    if spec.on_open is not None:
        await spec.on_open()
    await _consume(inbox_route(spec.name), _message_handler(spec))


async def _open_while_held(spec: InboxSpec, opener: _HeldOpener, let_go: asyncio.Event) -> None:
    while True:
        try:
            async with spec.consume_while():
                await _open(spec)
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


async def _open_inboxes_named_at_start() -> None:
    """调全部"启动时再开"的声明。有一个失败，这一次声明的收件箱全部撤掉、声明全部留着再抛。"""
    declared_before = dict(INBOX_REGISTRY)
    try:
        for open_them in INBOXES_AT_START:
            await open_them()
    except BaseException:
        INBOX_REGISTRY.clear()
        INBOX_REGISTRY.update(declared_before)
        raise
    INBOXES_AT_START.clear()


async def start_receiving() -> None:
    """开设本进程声明的全部收件箱并开始消费；同时消费本泳道的定时队列。

    每个收件箱建两条队列，问题队列马上开始消费。声明了 ``consume_while`` 的收件箱，普通消息在
    后台等到持有之后才开始消费，这里不等它。名字到启动时才知道的那几组先取名字，再开设
    （:func:`inboxes_at_start`）。
    """
    global _let_go, _stopping
    _stopping = False
    await _open_inboxes_named_at_start()
    _let_go = asyncio.Event()
    await mq.declare_route(SCHEDULED, lane=lane())
    await _consume(SCHEDULED, _on_scheduled)
    for spec in INBOX_REGISTRY.values():
        await mq.declare_route(inbox_route(spec.name), lane=lane())
        questions = question_route(spec.name)
        await mq.declare_route(questions, lane=lane())
        # 问题不等 on_open 和 consume_while（见模块说明）。
        await _consume(questions, _question_handler(spec))
        if spec.consume_while is None:
            await _open(spec)
            continue
        opener = _HeldOpener()
        opener.task = asyncio.create_task(
            _open_while_held(spec, opener, _let_go),
            name=f"messaging-open-{spec.name}",
        )
        _held_openers.append(opener)


async def stop_receiving() -> None:
    """停止消费：取消消费者，等正在处理的消息，等不完的放回去（见模块说明）。"""
    global _stopping
    _stopping = True
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


def _consumer(decide: Decide, *, questions: bool = False):
    """一条队列的消费回调：业务处理和记账交给 ``decide``，这里接住它抛出来的基础设施失败，再把
    结论交给 :func:`_settle`。``questions`` 说明这是一条问题队列：问题不重试，失败了确认掉。"""

    async def handler(incoming: AbstractIncomingMessage) -> None:
        task = asyncio.current_task()
        if questions:
            _answering.add(task)
        try:
            try:
                verdict = await decide(incoming)
            except asyncio.CancelledError:
                # 进程在停（:func:`stop_receiving`），或者通道被关掉了。普通消息：通道已经关了，
                # broker 把它放回原队列。问题：进程在停时通道还开着，确认掉，不退回队列；通道
                # 已经关了就确认不了（aio-pika 这时一碰 ``incoming.channel`` 就抛
                # ChannelInvalidStateError），broker 把它放回去，再来时按 id 认出处理过、确认掉。
                # 不管哪种，取消都原样往外抛。
                if questions:
                    with suppress(ChannelInvalidStateError):
                        await incoming.ack()
                raise
            except Exception:
                logger.warning(
                    "messaging: bookkeeping or hand-over for a delivery on %s failed; %s",
                    incoming.routing_key,
                    "acknowledged, a question is never handled twice"
                    if questions
                    else "putting it back",
                    exc_info=True,
                )
                verdict = Verdict.ACK if questions else Verdict.PUT_BACK
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
    """领取 → 业务处理 → 标记，整体最多 :data:`DELIVERY_DEADLINE`。只有 ``run`` 的失败算业务失败，
    交给 ``after_failure``；别的步骤抛出去，由 :func:`_consumer` 按基础设施失败处理。

    按消息 id 去重：处理成功过的不再处理；另一个进程正拿着（租约没过期）的，按剩下的租约延时
    重新排回去——那个进程要是半路死了，租约过期后由这里接管。每次占位用一个只属于这一次的
    标记：租约过期被别人接管之后，这里的成功或失败都不再改那一行，也不再重投。

    被取消时放开占位（:func:`_release`），不管取消落在哪一步：领取的事务可能已经提交、卡在之后
    关连接那一步（2026-10-06 在 coe 上就是这样），标记的那一笔可能还没落下。不放开的话，占位一直
    是"处理中"，重投的那一份要等租约过期才有人接。放开按这次占位的标记做，没占上的、已经记下了
    结果的都不改；标记成功之前被取消的，重投时处理函数会再看到它一次。到了期限也是这样取消、放开，
    然后只把这一条放回原队列，通道照常开着。
    """
    claim_token = f"{WORKER_ID}#{uuid.uuid4().hex[:12]}"
    try:
        async with asyncio.timeout(DELIVERY_DEADLINE.total_seconds()) as deadline:
            try:
                return await _claim_run_settle(
                    message,
                    received,
                    claim_token,
                    route=route,
                    edge_id=edge_id,
                    lease_ms=lease_ms,
                    run=run,
                    after_failure=after_failure,
                )
            except asyncio.CancelledError:
                await _release(edge_id, message, claim_token, _cancel_cause(deadline))
                raise
    except TimeoutError:
        if not deadline.expired():
            raise
        logger.warning(
            "messaging: %s %s ran past its delivery deadline (%s); put back on its queue",
            edge_id,
            message.message_id,
            DELIVERY_DEADLINE,
        )
        return Verdict.PUT_BACK


async def _claim_run_settle(
    message: Message,
    received: dict[str, Any],
    claim_token: str,
    *,
    route,
    edge_id: str,
    lease_ms: int,
    run: Callable[[], Awaitable[None]],
    after_failure: Callable[[Message, dict[str, Any], Any, str, Exception], Awaitable[Verdict]],
) -> Verdict:
    """:func:`_handle` 的那三步，用 ``claim_token`` 占位。"""
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


def _cancel_cause(deadline: asyncio.Timeout) -> str:
    """一次处理为什么被取消，记进占位和日志：到了期限、进程在停，还是所在的通道被关掉了。"""
    if deadline.expired():
        return f"cancelled at its delivery deadline ({DELIVERY_DEADLINE}), to be redelivered"
    if _stopping:
        return "cancelled while the process was stopping"
    return (
        "cancelled because the channel it came on was closed "
        "(by the broker, or its connection dropped)"
    )


async def _release(edge_id: str, message: Message, claim_token: str, why: str) -> None:
    """这一次处理被取消了（原因 ``why``，见 :func:`_cancel_cause`）：按这次占位的标记放开它，
    重投的那一份马上有人接。没占上的、已经记下了结果的不改；放不开、或者
    :data:`RELEASE_SECONDS` 之内没放开，只记一笔，占位留到租约过期。通道不是因为进程在停而关掉的，
    关闭原因在 :func:`_closed_while_consuming` 记的那条 warning 里。
    """
    try:
        async with asyncio.timeout(RELEASE_SECONDS):
            released = await mark_failed(
                edge_id=edge_id,
                idempotent_key=message.message_id,
                last_error=why,
                worker_id=claim_token,
            )
    except Exception:
        logger.warning(
            "messaging: %s %s was %s, and its claim could not be released",
            edge_id,
            message.message_id,
            why,
            exc_info=True,
        )
        return
    logger.log(
        logging.INFO if _stopping else logging.WARNING,
        "messaging: %s %s was %s; %s",
        edge_id,
        message.message_id,
        why,
        "its claim is released" if released else "it held no claim to release",
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


def _inbox_edge(name: str) -> str:
    """一个收件箱（普通消息和问题共用）的去重状态 edge。"""
    return _edge(f"inbox:{name}")


async def succeeded_message_ids(inbox_name: str, message_ids: Iterable[str]) -> set[str]:
    """``message_ids`` 里，在本泳道名为 ``inbox_name`` 的收件箱已经有一次投递处理成功、而且成功
    记下来了的那些。

    记下来的成功不会再变：同一条消息之后再来的投递，在交给处理函数之前就被挡掉（见模块说明
    "至少一次，按消息 id 去重"）。**处理函数返回不等于记下来了**：成功是返回之后才记的
    （:func:`_handle`），中间进程死了或者那一笔没写成，这次投递放回去，租约过期后同一条还会再交到
    处理函数手里。拥有者自己记着"处理过哪些"、想知道什么时候可以不记时，以这里为准，不以处理函数
    返回为准。
    """
    ids = list(dict.fromkeys(message_ids))
    if not ids:
        return set()
    return await succeeded_keys(edge_id=_inbox_edge(inbox_name), idempotent_keys=ids)


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

    **先记为已处理，再答。** 领到之后立刻把占位收成"处理过"，然后才调回答函数。这样不存在
    "回答已经执行、但还没记为已处理"的时间窗口：不管回答时失败、进程停下被取消，还是确认之后
    又来了同 id 的副本，这个问题都不会被再领一次。代价是记不成的时候（抛出去，最外层确认掉）
    这一次就不答——问题本来就不重试，提问方拿到"没有回答"。
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


def _message_handler(spec: InboxSpec):
    """收件箱本身那条队列：普通消息和退回的告知，交给 ``on_message``。"""
    route = inbox_route(spec.name)
    edge_id = _inbox_edge(spec.name)
    return _consumer(lambda incoming: _deliver_to_owner(spec, route, edge_id, incoming))


def _question_handler(spec: InboxSpec):
    """收件箱旁边的问题队列：交给 ``on_question``。去重跟普通消息用同一个 edge，靠消息 id 区分。"""
    route = question_route(spec.name)
    edge_id = _inbox_edge(spec.name)
    return _consumer(
        lambda incoming: _answer_question(spec, route, edge_id, incoming), questions=True
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
