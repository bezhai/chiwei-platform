"""world 收件箱的处理：送来的消息先收下，一轮处理收件箱里所有还没经过一轮的消息，一次只跑一轮。

**为什么不是一条消息一轮。** 三姐妹每一轮都给 world 发一条汇总，姐妹聊天时 world 几乎一轮接
一轮，每一轮都把同一份历史整个读一遍。world 跑着的时候到的、同时到的几条汇总合进一轮，这份
开销只花一次（2026-10-06 定）。代价是一轮的输入里有好几条消息，主 agent 和感知判断看到的是这一
轮的全部，每条带着发件人和时间（:func:`app.world.main_agent.run_round`）。

**一次投递怎么处理**（:meth:`Rounds.receive`，就是 world 收件箱的处理函数）：

1. 交给各知识来源收下（:func:`app.world.sources.take_in`，按消息 id 去重）；
2. 某一轮已经处理完了的（:func:`app.world.pending.is_handled`），跳过；
3. 收进还没经过一轮的消息里（:func:`app.world.pending.add`，记在私有卷上）；
4. 不叫醒 world 的消息到这里就算处理完了：它不单独起一轮，等下一轮一起看；
5. 别的等"下一轮"跑完：这一轮成功，这次投递就处理完了；失败，这次投递就算处理失败，原样交给
   通信机制重试（最新的自定醒来不限次数，:func:`app.world.wake.retry_latest_wake_without_limit`）。
   等着的时候前面那一轮失败、把这条消息放弃了（见下面"放弃"），这次投递也算处理失败：它没被
   处理过，不能确认掉，再投来时重新收下。

**自定醒来是不是旧的，只在一轮开始那一刻（拿着锁）判断，不在投递一到就判断。** 状态里的最新
唤醒只在两处改：一轮收尾时（:func:`app.world.main_agent.run_round` 调
:func:`app.world.wake.set_next_wake`，这一轮在锁里跑），和收件箱开始消费之前的启动补醒
（:func:`app.world.wake.wake_on_start`）。``set_next_wake`` 先发后记：新的醒来排出去、broker 确认、
通信机制记完账之后才记成最新唤醒，这一段里新时刻要是已经到了，那条醒来会先送到。投递一到就判，
这时状态还落后一步，它就会被当成旧消息确认掉，状态随后记的却正是它——world 再也不醒。拿着锁的
时候没有哪一轮在收尾，状态不会落后：送到的醒来比状态新，只可能是排它的那一轮在"记下来"之前就
失败了，那它确实是旧的。不能为了这个改成先记后发：记了没排出去的唤醒，就是一个不会来的醒来。

**"下一轮"。** 同一时刻只有一轮在跑（一把进程内的锁；收件箱只在拿着卷的写锁的那个进程里
消费，见 :mod:`app.world.wiring`）。一轮在跑的时候到的那几次投递，第一次去排队等这一轮跑完、
由它来跑下一轮，后面的跟着等同一个下一轮。下一轮开始那一刻（拿到锁），读出还没经过一轮的全部
消息——不只是正在等它的这几次投递带来的，也有不叫醒的、以及之前失败的轮里留下的、投递还在
重试的——一起跑；这一刻之后到的，等再下一轮。所以一轮进行中到的几条在下一轮里一起出现，只跑
一轮，它们各自的投递不再各起一轮。开始那一刻还会把成了旧消息的自定醒来拿掉（排着的时候前一轮
定了新的时刻）。等着这一轮的投递送来的消息要是都不在剩下的里面了（成了旧的醒来，或者排着的
时候前一轮已经把它处理完了），这一轮就不跑：旧的醒来、已经处理完的消息不叫醒 world，剩下的那些
等它们自己的投递，或者下一次真正要一轮的投递。

**一条消息什么时候算处理完。** 带着它的那一轮跑完了（主 agent 定了下次醒来、上下文存下、新的醒来
排出去之后），才记成处理完（:func:`app.world.pending.handled`），等它的那几次投递这时才处理成功。
这条记录留到通信机制把它的投递成功记下来为止，每一轮开始时问一次、删掉已经记下来的（为什么不能
在处理函数返回时删、文件有多大，见 :mod:`app.world.pending`）。
一轮失败或者被取消，这一轮带着的消息都还在还没经过一轮的那些里，之后的一轮照样带上；等它的投递
全部算处理失败，各自重试，不会有一次投递把没跑完的消息当成处理完了确认掉。之后某一轮已经处理
完了它，它的重试再来就在第 2 步跳过。

**一直跑不完的消息放弃。** 一轮因为异常没跑完，这一轮带着的每条记一次失败；一条消息失败的轮数跟
通信机制给一次投递的处理次数（:data:`app.messaging.receiving.PROCESSING_RETRY`）一样多，就从还没
经过一轮的里拿掉，之后的轮不再带它（:func:`app.world.pending.failed`，world 自己排的醒来除外）。
这是防一条每次都让一轮跑不完的消息（比如模型拒收它的内容）把之后的每一轮都拖住；一条消息一轮时，
它的投递处理四次失败就进死信，world 照常往下跑，这里是同一个上限。放弃只是拿掉：它之后再有投递
（死信重放、它自己还没用完的重试）就重新收下、重新数，下一轮照样带上；不叫醒的消息没有死信，
放弃时那条 error 里的整条消息是唯一能照着重做的东西，重做要换新的消息 id（原 id 送到时就记成
处理成功了，按原 id 重发会被通信机制挡掉，见 :mod:`app.world.pending`）。代价是这几轮里同时在等的别的消息也各记了
失败，失败的原因要是出在别处（模型那边临时出错），它们会跟着被放弃——跟一条消息一轮时那几条
各自的投递四次失败、进死信是一回事，它们的重试和死信重放照样把它们带回来。一轮超过时限算失败；
进程在停、一轮被取消，不算失败。

**一次投递最多等多久**（:attr:`Rounds.delivery_timeout`）：前面正在跑的一轮，加上带着它的这一轮。
一轮的时限由这里管（``round_timeout``，超过就取消、算这一轮失败），收件箱的处理时限按这个放长，
占位租约随之放长，所以等着的投递不会被当成"前一个进程死了"接管。这段时间里投递一直没确认：它
加上领取和记结果的余量，要放进通信机制给一次投递的期限
（:data:`app.messaging.receiving.DELIVERY_DEADLINE`，比 broker 等确认的时限短；放不进去开设收件箱时
就拒绝），所以一轮的时限不能太长（见 :data:`app.world.main_agent.ROUND_TIMEOUT`）。代价在进程直接死掉的时候：它
拿着的投递被 broker 重投给下一个进程，下一个进程要等那份占位租约过期才接手，现在最长约 22 分钟
（一轮 10 分钟乘二，加两次各一分钟的余量）。正常停下、通道被关掉都不受影响：被取消时先放开
占位，重投的那一份马上有人接。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from app.messaging import receiving
from app.messaging.message import Message
from app.world import pending
from app.world.sources import take_in
from app.world.wake import WORLD, is_own_wake, is_stale_wake

logger = logging.getLogger(__name__)

RunRound = Callable[[Sequence[Message]], Awaitable[None]]

# 一次投递等完两轮之后，把结果交回通信机制还要一点时间（记成处理完、排重试）。
_SETTLING = timedelta(minutes=1)

# 每一轮开始时问通信机制"哪些处理完的消息已经记下了成功"，最多等多久
# （:meth:`Rounds._forget_settled`）。正常是一条走索引的查询，几毫秒就回来；库那边卡住时，它拿着锁，
# 这一轮和排在后面的投递全都跟着等。等不到就按"这次没查到"处理：记录留着，这一轮照常跑，下一轮
# 再问。一次投递最多经过两次这一步，加起来远小于 :data:`_SETTLING`。
SETTLED_QUERY_TIMEOUT = timedelta(seconds=10)


class RoundFailed(RuntimeError):
    """带着这条消息的那一轮没跑完：这次投递算处理失败，按通信机制的重试再来。"""


@dataclass
class _NextRound:
    """排在正在跑的那一轮后面的下一轮：等它的投递在这里等它跑完。"""

    # 在等这一轮的那几次投递送来的消息 id。
    waiting: set[str] = field(default_factory=set)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    failure: BaseException | None = None


class Rounds:
    """world 的一轮接一轮。``run`` 带着一轮的消息（按到达的先后）跑一轮，失败就抛异常。"""

    def __init__(self, run: RunRound, *, round_timeout: timedelta) -> None:
        self._run = run
        self._round_timeout = round_timeout
        self._lock = asyncio.Lock()
        # 还没开始的下一轮；没有就是 None，下一次要等一轮的投递来起一个。
        self._next: _NextRound | None = None

    @property
    def delivery_timeout(self) -> timedelta:
        """一次投递最多处理多久：前面正在跑的一轮，加上带着它的这一轮。"""
        return 2 * self._round_timeout + _SETTLING

    async def receive(self, message: Message) -> None:
        """world 收件箱的处理函数：收下这条消息，要叫醒 world 的就等一轮把它处理完。"""
        await take_in(message)
        if pending.is_handled(message.message_id):
            logger.info(
                "world: message %s was handled by an earlier round; skipped",
                message.message_id,
            )
            return
        pending.add(message)
        if not message.wakes_recipient:
            logger.info(
                "world: message %s does not wake world; the next round takes it",
                message.message_id,
            )
            return
        await self._through_next_round(message.message_id)
        if not pending.is_handled(message.message_id) and not is_own_wake(message):
            # 它在等的时候，正在跑的那一轮失败、把它放弃了：这次投递不能当成处理完。算处理失败，
            # 通信机制再投时重新收下（自定醒来不会被放弃，没处理就是成了旧的、被拿掉了）。
            raise RoundFailed(
                f"message {message.message_id} was given up while this delivery waited"
            )

    async def _through_next_round(self, message_id: str) -> None:
        upcoming = self._next
        if upcoming is None:
            upcoming = self._next = _NextRound(waiting={message_id})
            await self._lead(upcoming)
            return
        upcoming.waiting.add(message_id)
        await upcoming.finished.wait()
        if upcoming.failure is not None:
            raise RoundFailed(
                f"the round that took this message did not finish: "
                f"{type(upcoming.failure).__name__}: {upcoming.failure}"
            ) from upcoming.failure

    async def _lead(self, upcoming: _NextRound) -> None:
        """等正在跑的那一轮跑完，跑下一轮；结果交给跟着等的那几次投递。"""
        try:
            async with self._lock:
                await self._forget_settled()
                self._next = None  # 从这一刻起到的，等再下一轮
                await self._run_waiting(upcoming.waiting)
        except BaseException as exc:
            upcoming.failure = exc
            raise
        finally:
            if self._next is upcoming:  # 还没拿到锁就被取消了
                self._next = None
            upcoming.finished.set()

    async def _forget_settled(self) -> None:
        """问通信机制：记着处理完的那些里，哪些的投递成功已经记下来了，删掉它们的记录。问不到、
        或者 :data:`SETTLED_QUERY_TIMEOUT` 之内没有回音，就都留着，下一轮再问。"""
        recorded = pending.recorded()
        if not recorded:
            return
        try:
            async with asyncio.timeout(SETTLED_QUERY_TIMEOUT.total_seconds()):
                settled = await receiving.succeeded_message_ids(WORLD, recorded)
        except Exception:
            logger.warning(
                "world: could not ask messaging which of %d handled message(s) are settled; "
                "keeping their records until the next round",
                len(recorded),
                exc_info=True,
            )
            return
        pending.forget(settled)

    async def _run_waiting(self, delivered: set[str]) -> None:
        """跑一轮，带上还没经过一轮的全部消息。``delivered`` 是等着这一轮的投递送来的消息：要是
        它们都已经不在还没经过一轮的里面了（成了旧的醒来、已经被前一轮处理完，或者被放弃了），
        这一轮就不跑。"""
        waiting = pending.read()
        stale = [m for m in waiting if is_stale_wake(m)]
        if stale:
            pending.drop(m.message_id for m in stale)
            logger.info(
                "world: wake(s) %s were replaced while waiting; left out",
                ", ".join(m.message_id for m in stale),
            )
        taking = [m for m in waiting if m not in stale]
        if not any(m.message_id in delivered for m in taking):
            return
        ids = [m.message_id for m in taking]
        try:
            async with asyncio.timeout(self._round_timeout.total_seconds()):
                await self._run(taking)
        except Exception:
            pending.failed(ids, give_up_at=receiving.PROCESSING_RETRY.n)
            raise
        pending.handled(taking)
