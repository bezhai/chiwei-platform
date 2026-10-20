"""world 主 agent 的一轮：带着收件箱里还没经过一轮的消息，看记录、看现实、让世界变化，最后定
下次醒来的时刻。

**一轮带着哪些消息、什么时候跑**由 :mod:`app.world.rounds` 决定：一轮处理收件箱里所有还没经过
一轮的消息，同一时刻只有一轮在跑，一轮最多 :data:`ROUND_TIMEOUT`；这一轮的 id 和时刻也是它在
开始跑这一轮时定的。这里是一轮由什么组成：固定循环（:func:`app.agent.round.run_round`）按清单
``world`` 排的位置跑它们（:func:`run_inbox_round`），world 插件（:mod:`app.plugins.world`）登记。

**一轮开始前**（``unfinished.resend``，:func:`resend_left_over`）：之前有一轮没跑完时，按原 id
把那一轮里要发的告知全部补发一遍。发送出错这一轮在模型开始之前就算失败。

**一轮怎样才算跑完。** 模型那一段结束之后，依次：

1. 检查这一轮定了下次醒来的时刻（``wake.chosen``，:func:`app.world.actions.wake_me_at`）。没定就抛
   :class:`NoNextWake`，这一轮算没跑完。工具预算用完时框架给的那一次不带工具的收尾定不了
   时刻，同样按没定处理；
2. 把这一轮存进它的连续上下文（``history``，:mod:`app.agent.continuity`，按版本做 CAS），然后
   清空 :mod:`app.world.unfinished`（``unfinished.clear``）——这一轮发生过的事已经在上下文里了；
3. 定下次醒来（``wake.next``，:func:`app.world.wake.set_next_wake`：先排出自定消息，broker 确认之后
   才记成私有状态里的最新唤醒；跟已经排着、还没送到的那一条是同一个时刻，就不再排）。

没有落地事务。任何一步失败都往外抛，这一轮算没跑完，带着的消息都留给之后的一轮，等它们的投递
按通信机制重试（:mod:`app.world.rounds`）。再跑是安全的：它改过的记录留在盘上，下一次读得到；
已经发出去的告知收不回来：报告过的变化、出过场的 NPC 和要发的告知（带预先定好的消息 id）在发之前
就记在 :mod:`app.world.unfinished`，下一轮模型跑之前按原 id 补发一遍（接收方按 id 去重），再摆到它
眼前，它不会把同一件事再报告一遍；上下文和下次醒来都只在最后才写下。这一轮带着的自定消息在
第 3 步记下新唤醒之前一直是状态里的最新唤醒，所以重投时不会被当成旧消息，失败了也不限次数
重试、不进死信；哪一处失败、进程死在哪里，各自怎么接上见 :mod:`app.world.wake`。先存上下文、
后定时刻，是因为定时刻做完之后这一轮就不该再重跑——否则会多出一个被取代的自定消息，而上下文
里又少了这一轮。

**它眼前摆着什么。** 一条 USER 消息，三个输入段用一个换行拼起来：现在几点和这一轮带着的消息
（``wake.woken_by``，按到达的先后，每条是谁的、什么时候的、原文）；里面没有它给自己排的醒来时，
它原来定的下次醒来，提醒它这一轮结束前要重新定（``wake.planned``）；之前有一轮没跑完时，那一轮
里已经发生的事和补发告知的结果（``unfinished.happenings``）。它的记录目录是状态段
（``records.listing``），只在上下文清理时写进那条带时刻的标记消息（:mod:`app.agent.continuity`；
每轮都一样的东西不每轮重发）。prompt 在 Langfuse（:data:`ROUND`），正文不引用任何变量。

**它手里有什么。** 清单 ``world`` 排的工具：三个知识来源插件的查询工具（records、reality、told，
关掉的来源由一轮开始时读的开关拿掉），加上只有它才有的动作（:mod:`app.world.actions`）。来源的
查询结果是读到的材料，按 :data:`TRIM_POLICY` 的短档保留。

**模型和工具预算走 Dynamic Config**，跟另外三类一起在 :mod:`app.world.agents`。裁剪阈值
（:data:`TRIM_POLICY`）是这个 agent 的一部分，写死在这里。
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.agent.continuity import TrimPolicy
from app.agent.round import AssembledProfile, Round, run_round
from app.messaging.message import Kind, Message
from app.world import records, unfinished
from app.world.actions import RoundScope
from app.world.agents import when, world_agent
from app.world.perception import Told, render_told, send_notices
from app.world.rounds import InboxRound
from app.world.wake import WORLD, NextWake, is_own_wake, read_next_wake, set_next_wake

logger = logging.getLogger(__name__)

# 主 agent 这一类，模型用主 agent 的键。它的 prompt 按顶层小节分成八段（Langfuse id
# ``world_round/<段名>``，泳道 label 取不到时退回 production，见 :class:`app.agent.prompts.PromptSet`），
# 按这个顺序用空行拼成一条 SYSTEM，和分段之前的整份逐字节相同。身份、「你能查到什么」、「什么会让世界
# 变化」、「你改什么，不改什么」在核心那一段；「你能查到什么」和「一轮的顺序」说到好几样东西，各自整段
# 留在一起。
ROUND = "world_round"
ROUND_SEGMENTS = tuple(
    f"{ROUND}/{name}"
    for name in (
        "core",
        "reality",
        "report-change",
        "npc",
        "write-record",
        "wake",
        "unfinished",
        "order",
    )
)
ROUND_AGENT = world_agent(ROUND, "world-round", segments=ROUND_SEGMENTS)

# 一轮最多跑多久，超过就取消、算这一轮没跑完（:class:`app.world.rounds.Rounds` 管它，收件箱的
# 处理时限和占位租约按它放长）。正常一轮 30 到 70 秒，10 分钟是将近十倍的余量。不能再长：一次投递
# 最多要等两轮加一分钟（21 分钟），再给领取和记结果留出余量，要放进通信机制给一次投递的期限
# （:data:`app.messaging.receiving.DELIVERY_DEADLINE`，28 分钟，比 broker 等确认的 30 分钟短）。
# 放不进去开设收件箱时就拒绝；以前是 broker 在 world 正常等轮的时候关掉整个通道，把同一通道上正在
# 跑的一轮也取消掉（2026-10-06 在 coe-world 上，这里是 30 分钟时）。
ROUND_TIMEOUT = timedelta(minutes=10)

# 读到的材料（记录、搜索、天气）过一个清理周期就换成一句"不在眼前了"，要用再读；它自己想过
# 说过的最多留半天。
#
# 带进一轮的历史压在两三万 token：估算超过 3 万就从最老的整组丢到 2 万以下。一轮里每调一次
# 模型都把这份历史整个再送一遍，费用跟它成正比。2026-10-05 在 coe-world 上量到：历史估
# 8.9 万时一次调用的输入是 9.35 万 token（另外是 SYSTEM 约 3.2k、工具定义约 1.5k），一轮调
# 3 到 5 次，列表价 1.4 到 2.3 美元；一轮在历史里平均留下约 1.7k token（中位 1.6k，最多 5k），
# 每跨一个清理点再加一条约 0.6k 的标记消息。所以 2 万到 3 万之间是十几轮。
#
# 被姐妹们的汇总叫醒时它一分钟就能跑一轮，先撞上的是这个顶；自己醒的时候两次常隔一两个小时，
# 半天里五六轮连同标记消息不到 3 万，那时起作用的是半天那一档。顶和目标之间留 1 万，是因为
# 每裁一次历史的开头就变了、那一轮的前缀缓存全部落空，1 万大约是六轮裁一次。
TRIM_POLICY = TrimPolicy(
    material_minutes=60,
    own_minutes=720,
    cleanup_minutes=60,
    hard_cap_tokens=30_000,
    trim_target_tokens=20_000,
)


def _listed(messages: Sequence[Message]) -> str:
    """日志里的一轮带着哪些消息：每条的 id、类型、发送方。"""
    return ", ".join(f"{m.message_id} ({m.kind} from {m.sender})" for m in messages)


class NoNextWake(RuntimeError):
    """这一轮跑完了模型那一段，却没有定下次醒来的时刻：这一轮不算完成。"""


@dataclass
class LeftOver:
    """这一轮的状态：没跑完的那一轮里已经发生的事，每件连同这一轮开始前补发它的告知的结果。"""

    happenings: list[tuple[unfinished.Happening, list[Told]]] = field(
        default_factory=list
    )


@dataclass
class Scheduled:
    """这一轮的状态：最后定下的下次醒来（:func:`set_the_next_wake`），写进收尾那行日志。"""

    wake: NextWake | None = None


async def resend_left_over(round: Round) -> None:
    """一轮开始前（``unfinished.resend``）：按原 id 把还没进上下文的那几件事要发的告知全部再发
    一遍，结果留给 :func:`left_over`。

    发送出错原样往外抛：这一轮在模型开始之前就算失败，记着的东西一样不动，下一轮再补。
    """
    left_over = [(h, await send_notices(h.notices)) for h in unfinished.read()]
    if left_over:
        logger.info(
            "world: resent %d notice(s) of %d happening(s) left by an unfinished round",
            sum(len(told) for _, told in left_over),
            len(left_over),
        )
    round.state(LeftOver).happenings = left_over


async def records_listing(round: Round) -> str:
    """状态段（``records.listing``）：它有哪些记录。只写进上下文清理的那条标记消息。"""
    entries = records.listing()
    if not entries:
        return "你还没有任何记录。"
    return "\n".join(
        [f"你的记录（{len(entries)} 份）："]
        + [f"- {e.path}（{e.chars} 字）" for e in entries]
    )


def _render_message(message: Message) -> str:
    """一轮带着的一条消息：谁的、什么时候的，下一行起是原文。"""
    if message.kind is Kind.NOT_DELIVERED:
        head = f"通信机制告诉你，你的一条消息没有送达（{when(message.time)}）："
    elif is_own_wake(message):
        head = f"你给自己排的一次醒来（排在 {when(message.time)}）："
    else:
        head = f"{message.sender} 发来一条消息（{when(message.time)}）："
    return f"{head}\n{message.body}"


async def woken_by(round: Round) -> str:
    """输入段（``wake.woken_by``）：现在几点，这一轮带着的消息，按到达的先后编号。"""
    messages = round.due.messages
    lines = [
        f"【现在】{when(round.now)}",
        f"【叫醒你的】这一轮有 {len(messages)} 条消息，按到达的先后：",
    ]
    lines += [
        f"（{number}）{_render_message(m)}" for number, m in enumerate(messages, 1)
    ]
    return "\n".join(lines)


async def planned_wake(round: Round) -> str | None:
    """输入段（``wake.planned``）：它原来定的下次醒来，提醒它这一轮结束前要重新定。还没定过、或者
    这一轮带着它给自己排的醒来时不摆。"""
    planned = read_next_wake()
    if planned is None or any(is_own_wake(m) for m in round.due.messages):
        return None
    return (
        f"【你原来定的下次醒来】{when(planned.at)}。当时的说明：{planned.reason}\n"
        f"这一轮结束前要重新定一个时刻；还想按原来的来，就再定一次同一个时刻。"
    )


async def left_over(round: Round) -> str | None:
    """输入段（``unfinished.happenings``）：没跑完的那一轮里已经发生的事和补发告知的结果
    （:func:`resend_left_over`）。没有时不摆。"""
    happenings = round.state(LeftOver).happenings
    if not happenings:
        return None
    lines = [
        "【之前没跑完的一轮里已经发生的事】之前有一轮没有正常收尾。那一轮的经过你可能记得，"
        "也可能不记得；不管记不记得，下面这些在那一轮里已经发生了，记录里可能还没写。那一轮"
        "要发的告知，这一轮开始前已经按原样补发了一遍（收到过的人不会再收到一遍），下面是"
        "补发的结果："
    ]
    lines += [
        f"- {when(h.at)}\n{h.what}\n{render_told(told)}" for h, told in happenings
    ]
    return "\n".join(lines)


async def next_wake_chosen(round: Round) -> None:
    """检查（``wake.chosen``）：这一轮定了下次醒来的时刻，没定抛 :class:`NoNextWake`。"""
    if round.state(RoundScope).next_wake is None:
        raise NoNextWake(
            f"world round {round.round_id} (taking {_listed(round.due.messages)}) ended "
            f"without setting its next wake"
        )


async def clear_unfinished(round: Round) -> None:
    """``unfinished.clear``：这一轮存进上下文了，没跑完的那一轮里发生的事不用再记着。"""
    unfinished.clear()


async def set_the_next_wake(round: Round) -> None:
    """``wake.next``：定下这一轮选的下次醒来（:func:`app.world.wake.set_next_wake`），这一轮带着的
    消息交给它判断要不要记成"已经醒过"。"""
    choice = round.state(RoundScope).next_wake
    round.state(Scheduled).wake = await set_next_wake(
        choice.at,
        f"你在 {when(round.now)} 定下这个时刻醒来，当时写下的理由：{choice.reason}",
        taken=round.due.messages,
    )


async def run_inbox_round(
    profile: AssembledProfile, due: InboxRound, *, round_id: str, now: datetime
) -> None:
    """带着 ``due`` 的消息（按到达的先后）按 ``profile``（清单 ``world``）跑一轮，``round_id`` 和
    ``now`` 是收件箱来源开始跑它时定的。没定下次醒来的时刻抛 :class:`NoNextWake`；别的失败原样
    往外抛。"""
    result = await run_round(
        profile,
        participant=WORLD,
        round_id=round_id,
        now=now,
        reason=due.reason,
        due=due,
    )
    chosen = result.round.state(Scheduled).wake
    logger.info(
        "world: round %s taking %s done; wrote %d record(s); next wake %s at %s; said: %s",
        round_id,
        _listed(due.messages),
        len(result.round.state(RoundScope).written),
        chosen.message_id,
        chosen.at.isoformat(),
        result.reply.text().strip()[:200],
    )
