"""被叫来提前的那一轮 —— 只提前，不代她回复。

私聊来了、群里被点名，她不该等最多十分钟才知道。所以多一条钟：一分钟一拍，看看有没有
**新**的一条在叫她；有就把她带到那一刻。

**「强」不是发送方标的。** 没有优先级表、没有权重、没有分级——那是替她做决定。这里只认
两条客观事实：

  * **私聊**：私聊本身就意味着有人在等她回；
  * **群里点了她的名**：那是直接叫她（``common_message.mentioned_common_user_ids``
    这一列装着被 @ 的人在公共层的 id，投影层落账时写下的，是库里的客观事实，不需要
    模型判断）。

群里不点名的消息不提前——它在下一个常规轮次照样被她看到，一条都不会丢。

**传到她这里的消息也提前**（:mod:`app.living.received`）：world 告诉她察觉到了什么、姐妹
直接对她说的话，有她还没看过的就把她带到那一刻。这里不分 world 还是姐妹、不分说了什么，
跟手机那两条一样只认"有一条她没看过"这个事实。手机上那条叫过她、她没看手机所以一直未读时，
它挡不住收件箱里的新消息：两边各认各的那条。

收件箱里取的是**最早**那条没看过的，不是最新那条：她那一轮按消息自带的时间从最早的摆起
（一轮最多摆 :data:`app.living.received.RECEIVED_LIMIT` 条），取最早那条，叫醒她的那条就
一定在那一轮里，那一轮落地它就算看过了。取最新那条的话，积压多时它可能排在一轮摆得下的范围
之外，那一轮叫过它、它却还没看过。

她正在跑一轮的时候，这条钟等那一轮结束才判（一轮最长占锁 15 分钟，见
:func:`app.living.moment.run_moment`）：那一轮跑着时到的消息没摆进去，还没看过，也还没叫醒过
她，所以一轮结束后的下一拍叫醒她一次。

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
from datetime import datetime
from typing import Annotated

from app.infra.cst_time import now_cst
from app.living.moment import LifeMoment, run_moment
from app.living.persona import LIVING_PERSONAS
from app.living.phone import newest_unread_summons
from app.living.received import unread_received
from app.living.records import living_lane
from app.runtime.data import Data, Key
from app.runtime.node import node

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


async def nudge_once(
    *, lane: str, persona_id: str, now: datetime
) -> LifeMoment | None:
    """有人在叫她、或者有传到她这里的消息她还没看过，就把她带到这一刻；都没有、或者都已经
    叫过了，返回 ``None``。

    先看手机：那条已经叫过她（``run_moment`` 交回 ``None``）不算数，接着看收件箱。

    返回值只回答"这一轮跑了没有"。**她回不回是她的输出**，不在这里判、也不该有人在
    这里判。
    """
    summons = await newest_unread_summons(
        lane=lane, persona_id=persona_id, now=now
    )
    if summons is not None:
        moment = await run_moment(
            lane=lane,
            persona_id=persona_id,
            now=now,
            nudged_by=summons.message_id,
        )
        if moment is not None:
            return moment
    earliest = await unread_received(lane=lane, persona_id=persona_id, limit=1)
    if not earliest:
        return None
    return await run_moment(
        lane=lane,
        persona_id=persona_id,
        now=now,
        nudged_by=f"{_INBOX}{earliest[0].message_id}",
    )


@node
async def phone_nudge_tick(tick: PhoneNudgeTick) -> None:
    """三个人各看一眼有没有人在叫自己。

    **并发跑，一个人炸不拖累另两个**（同 :func:`app.living.moment.life_moment_tick`）。
    跟固定那条钟并发打到同一个人时，两边在 :func:`app.living.serial.hold` 上排队——
    后到的等前一个做完，不是被丢掉。
    """
    lane, now = living_lane(), now_cst()
    outcomes = await asyncio.gather(
        *(
            nudge_once(lane=lane, persona_id=persona_id, now=now)
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
