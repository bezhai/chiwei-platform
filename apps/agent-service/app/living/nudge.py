"""被叫来提前的那一轮 —— 只提前，不代她回复。

私聊来了、群里被点名，她不该等最多十分钟才知道。所以多一条钟：一分钟一拍，看看有没有
**新**的一条在叫她；有就把她带到那一刻。

**手机上的「强」不是发送方标的。** 没有优先级表、没有权重、没有分级——那是替她做决定。这里只认
两条客观事实：

  * **私聊**：私聊本身就意味着有人在等她回；
  * **群里点了她的名**：那是直接叫她（``common_message.mentioned_common_user_ids``
    这一列装着被 @ 的人在公共层的 id，投影层落账时写下的，是库里的客观事实，不需要
    模型判断）。

群里不点名的消息不提前——它在下一个常规轮次照样被她看到，一条都不会丢。

**传到她这里的消息也提前**（:mod:`app.living.received`）：world 告诉她察觉到了什么、姐妹
直接对她说的话，有她还没看过、**发件方要叫醒她**的，就把她带到那一刻。要不要叫醒是发件方在
消息上说的（通信机制信封上的 ``wakes_recipient``）：world 的每条告知由 world 的感知判断自己定，
姐妹直接说的话都叫醒她。这里不分 world 还是姐妹、不分说了什么，只认"有一条要叫醒她的她没看过"
这个事实，不在这里另判哪条急。发件方说不叫醒的，不提前，照样摆进她下一轮（常规的那一轮，或者
被别的叫醒的那一轮），看过就算看过。手机上那条叫过她、她没看手机所以一直未读时，它挡不住收件箱
里的新消息：两边各认各的那条。

收件箱里取的是**最早**那条没看过、要叫醒她的，不是最新那条：她那一轮按消息自带的时间从最早的
摆起（一轮最多摆 :data:`app.living.received.RECEIVED_LIMIT` 条），积压的时候最早的先被看掉，
后面的接着一条一条叫醒她。"要叫醒她"在查询里就筛掉不叫醒的，不是先取最早那条再看：那样一条更早
的不叫醒的会一直占住最前面，挡住后面所有该叫醒她的。叫醒她的那条**一定**在那一轮里（``must_show``，见
:func:`app.living.moment.run_moment_held`）：叫醒她之后才到、发生得更早的消息再多，也挤不掉
它。那一轮的身份就是它，那一轮落地它就算看过；不然它会一直排在最前，每一拍拿它叫醒她都撞上
"这一轮跑过了"，收件箱的提前叫醒就卡在那儿。

**判"是什么叫醒了她"和跑那一轮在同一次占用里**（:func:`app.living.moment.life_moment_lock_key`）。
她正在跑一轮的时候，这条钟排一拍在后面，等那一轮结束才判（一轮最长占锁 15 分钟，见
:func:`app.living.moment.run_moment`）。判在占用外面的话，判完到轮到她之间隔着的那一轮会把
判据改掉（看过了、跑过了），判出来的是过期的结论。那一轮跑着时到的消息没摆进去，还没看过，
也还没叫醒过她，所以一轮结束后的下一拍叫醒她一次。

**排在后面的只有一拍**（:func:`app.living.serial.at_most_one`）。钟一分钟一拍、不等上一拍
跑完，她那一轮挂住的十五分钟里每一拍都排进去的话，那一轮放开之后它们一个接一个判、一个接一个
记日志，挂住接连发生时越积越多。已经有一拍在排队（或者正在跑它叫醒的那一轮）时，这一拍直接
过去：排着的那一拍轮到她时判的是那一刻的样子，后面那几拍要判的它都判得到。判还是在占用里，
上面那个"判完到轮到她之间被改掉"的问题不会回来。

**开始了却没落地的那一轮，下一拍重跑的还是它自己**（:class:`NudgeBegun`）。她在那一轮里做的
事、发出去的话，id 都从那一轮的身份派生（见 :func:`app.living.moment.run_moment`）；那一轮
在她已经做了些什么之后失败、超时、赶上部署，重跑时换了身份，同一个动作就落两遍、同一句话就
对真人发两遍。所以开跑之前先记下"被什么叫醒的那一轮开始了"，之后每一拍先看有没有开始了而
没落地的那一轮，有就原样重跑它，不管这期间手机上来了更新的一条、收件箱里到了更早的一条。
这是工程上的幂等，不是替她做决定：重跑的那一轮里她照样看得到这期间新到的一切，怎么做由她；
叫她的那些等它落地之后照常叫醒她。

**这条钟只看她视野里的会话**（:mod:`app.living.whitelist`）：名单外的那些整个不进她
视野，那里的一次点名也把她带不到那一刻。这一拍算出来的名单跟随后被唤醒的那一轮**是
两次独立计算**（中间隔着模型调用的时间），不是同一个快照——这条钟只决定"要不要把她带
到这一刻"，一轮之内那份锚由那一轮自己定。

**"她刚才把注意力放在哪"不在这里算。** 五分钟前刚在群里说过话、还是三天没说话，这条
事实原样摆进信封里给她看（见 :mod:`app.living.phone`），由她自己判算不算数。在这里
算成一个分数，就是把她的判断搬到代码里。

**提前只到"她被带到那一刻"为止。** 这里不看她说了什么、不判断她该不该回。「注意到」和
「开口」离得太近，一不小心 @ 就又变成回复开关——所以这条线在这里被切断：
:func:`nudge_once` 的返回值只说明"这一轮跑了没有"，跟她开没开口没有任何关系。

**同一条消息只把她叫来一次。** 她没看手机的话那条一直未读，按"还有没有未读"判就是每
分钟震一次、一天一千多次模型调用。做法不是加冷却，而是让**那一轮的身份就是那条消息**
（``nudge:<message_id>``）：跑过就是跑过了，新消息才是新的一轮。真人手机就是这样——
新消息才震，躺着的未读不会一直震。收件箱里的消息同一个做法，身份是
``nudge:inbox:<消息 id>``：前缀把它跟手机消息的 id 分开，两边的 id 不是同一套。同一条重投
一遍只存一行（:func:`app.living.received.receive`），也就不会再叫醒她。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from sqlalchemy import text

from app.data.session import get_session
from app.infra.cst_time import now_cst
from app.living.moment import (
    LifeMoment,
    life_moment_lock_key,
    moment_ran,
    nudged_moment_id,
    run_moment_held,
)
from app.living.persona import LIVING_PERSONAS
from app.living.phone import newest_unread_summons
from app.living.received import unread_received
from app.living.records import living_lane
from app.living.serial import at_most_one, hold
from app.runtime.data import Data, Key
from app.runtime.migrator import _table_name
from app.runtime.node import node
from app.runtime.persist import insert_append

logger = logging.getLogger(__name__)

# 一分钟一拍。这一拍绝大多数时候只是几次读库 + 比大小，一句模型都不调；拍得密只是让
# "有人叫她"到"她被带到那一刻"之间的延迟压在一分钟内（常规轮次是十分钟）。
PHONE_NUDGE_TICK_SECONDS = 60

# 收件箱里的消息叫醒她时，那一轮身份里 id 前面的这一截（见模块说明）。
_INBOX = "inbox:"


class PhoneNudgeTick(Data):
    """看一眼有没有人在叫她的那一拍。

    单字段 ``ts``——框架源循环固定按 ``data_type(ts=<iso>)`` 造 payload，多一个必填
    字段就是每一拍 ValidationError **直接杀 Pod**。这条约定顺带也是"chat 没有入口"
    的物理保证：钟装不下内容，就当不了信箱。
    """

    ts: Annotated[str, Key]

    class Meta:
        transient = True


class NudgeBegun(Data):
    """被什么叫醒的那一轮开始了：模型还没调、她还什么都没做的那一刻记下。

    自然键 ``(lane, persona_id, nudged_by)``：一个身份只开始一次。没落地的那一轮由
    :func:`nudge_once` 原样重跑，不再开始新的一轮。纯 append：落没落地不记在这一行上，看的是
    那一轮的 ``LifeMoment`` 在不在（:func:`app.living.moment.moment_ran`），那一轮落地的那次
    提交就是它的了结，不用回来改这一行。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    nudged_by: Annotated[str, Key]

    class Meta:
        # 读侧唯一形状：这个人最近开始的那一轮。
        indexes = (("lane", "persona_id", "created_at"),)


_BEGUN_TABLE = _table_name(NudgeBegun)


async def nudge_once(
    *, lane: str, persona_id: str, clock: Callable[[], datetime]
) -> LifeMoment | None:
    """有人在叫她、或者有传到她这里的消息她还没看过，就把她带到这一刻；都没有、或者都已经
    叫过了，返回 ``None``。

    整段在她的 moment 占用里：先看有没有开始了而没落地的那一轮，有就原样重跑它；没有再看
    现在有什么在叫她（:func:`_calling_her`），记下这一轮开始了，再跑。"现在"是拿到占用那一刻
    的钟（``clock()``），判"什么在叫她"和跑那一轮用的是同一个（理由见
    :func:`app.living.moment.run_moment`）。

    这条钟在她身上只排一拍（:func:`app.living.serial.at_most_one`）：已经有一拍在排队、或者
    正在跑它叫醒的那一轮，这一拍直接返回 ``None``，不排到后面去。排着的那一拍轮到她时才判，
    这一拍要判的它都判得到；那一轮跑着时才到、没摆进去的，那一轮之后的下一拍叫醒她。

    返回值只回答"这一轮跑了没有"。**她回不回是她的输出**，不在这里判、也不该有人在
    这里判。
    """
    key = life_moment_lock_key(lane, persona_id)
    async with at_most_one(f"{key}:nudge") as admitted:
        if not admitted:
            return None
        async with hold(key):
            return await _nudge_held(lane=lane, persona_id=persona_id, now=clock())


async def _nudge_held(*, lane: str, persona_id: str, now: datetime) -> LifeMoment | None:
    """:func:`nudge_once` 占住之后的那一段：判是什么叫醒了她，再跑那一轮。"""
    nudged_by = await _begun_not_landed(lane=lane, persona_id=persona_id)
    if nudged_by is None:
        nudged_by = await _calling_her(lane=lane, persona_id=persona_id, now=now)
        if nudged_by is None:
            return None
        await insert_append(
            NudgeBegun(lane=lane, persona_id=persona_id, nudged_by=nudged_by)
        )
    return await run_moment_held(
        lane=lane,
        persona_id=persona_id,
        now=now,
        nudged_by=nudged_by,
        must_show=(
            nudged_by.removeprefix(_INBOX) if nudged_by.startswith(_INBOX) else None
        ),
    )


async def _begun_not_landed(*, lane: str, persona_id: str) -> str | None:
    """最近开始的那一轮要是还没落地，它是被什么叫醒的；没有这样的一轮返回 ``None``。

    只看最近开始的那一轮就够：有一轮开始了而没落地时，:func:`nudge_once` 只重跑它、不开始
    新的一轮，所以没落地的至多一轮，而且就是最近开始的那一轮。
    """
    sql = (
        f"SELECT nudged_by FROM {_BEGUN_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        f"ORDER BY created_at DESC LIMIT 1"
    )
    async with get_session() as s:
        nudged_by = (
            await s.execute(text(sql), {"lane": lane, "persona_id": persona_id})
        ).scalar_one_or_none()
    if nudged_by is None or await _woke_her(
        lane=lane, persona_id=persona_id, nudged_by=nudged_by
    ):
        return None
    return nudged_by


async def _calling_her(*, lane: str, persona_id: str, now: datetime) -> str | None:
    """现在有什么在叫她、而且还没叫醒过她：先看手机，再看收件箱；都没有返回 ``None``。

    手机上那条已经叫醒过她（她没看手机，所以它一直是最新那条未读）不算数，接着看收件箱。
    收件箱里看的是最早那条要叫醒她、她还没看过的。它按理不会已经叫醒过她（叫醒她的那一轮落地时
    它一起算看过），照样问一句：改成这样之前的版本留下过"那一轮落地了、它还没看过"的状态，不问
    的话每一拍都会拿它再开始一轮，撞上已经记过的"开始了"。
    """
    summons = await newest_unread_summons(
        lane=lane, persona_id=persona_id, now=now
    )
    if summons is not None and not await _woke_her(
        lane=lane, persona_id=persona_id, nudged_by=summons.message_id
    ):
        return summons.message_id
    earliest = await unread_received(
        lane=lane, persona_id=persona_id, limit=1, waking_only=True
    )
    if not earliest:
        return None
    nudged_by = f"{_INBOX}{earliest[0].message_id}"
    if await _woke_her(lane=lane, persona_id=persona_id, nudged_by=nudged_by):
        return None
    return nudged_by


async def _woke_her(*, lane: str, persona_id: str, nudged_by: str) -> bool:
    """被这一条叫醒的那一轮落地了吗。"""
    return await moment_ran(
        lane=lane, persona_id=persona_id, moment_id=nudged_moment_id(nudged_by)
    )


@node
async def phone_nudge_tick(tick: PhoneNudgeTick) -> None:
    """三个人各看一眼有没有人在叫自己。

    **并发跑，一个人炸不拖累另两个**（同 :func:`app.living.moment.life_moment_tick`）。
    跟固定那条钟并发打到同一个人时，两边在 :func:`app.living.serial.hold` 上排队——
    后到的等前一个做完，不是被丢掉。
    """
    lane = living_lane()
    outcomes = await asyncio.gather(
        *(
            nudge_once(lane=lane, persona_id=persona_id, clock=now_cst)
            for persona_id in LIVING_PERSONAS
        ),
        return_exceptions=True,
    )
    for persona_id, outcome in zip(LIVING_PERSONAS, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.warning(
                "living nudge lane=%s persona=%s 这一轮炸了：%r",
                lane,
                persona_id,
                outcome,
                exc_info=outcome,
            )
        elif outcome is not None:
            logger.info(
                "living nudge lane=%s persona=%s 被叫来提前一轮（%s），说：%s",
                lane,
                persona_id,
                outcome.moment_id,
                outcome.said,
            )
