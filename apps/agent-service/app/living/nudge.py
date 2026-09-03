"""被叫来提前的那一缝 —— 只提前，不代她回复。

私聊来了、群里被点名，她不该等最多十分钟才知道。所以多一条钟：一分钟一拍，看看有没有
**新**的一条在叫她；有就把她带到那一刻。

**「强」不是发送方标的。** 没有优先级表、没有权重、没有分级——那是替她做决定。这里只认
两条客观事实：

  * **私聊**：私聊本身就意味着有人在等她回；
  * **群里点了她的名**：那是直接叫她（``common_message.mentioned_common_user_ids``
    这一列装着被 @ 的人在公共层的 id，投影层落账时写下的，是库里的客观事实，不需要
    模型判断）。

群里不点名的消息不提前——它在下一个常规缝照样被她看到，一条都不会丢。

**"她刚才把注意力放在哪"不在这里算。** 五分钟前刚在群里说过话、还是三天没说话，这条
事实原样摆进信封里给她看（见 :mod:`app.living.phone`），由她自己判算不算数。在这里
算成一个分数，就是把她的判断搬到代码里。

**提前只到"她被带到那一刻"为止。** 这里不看她说了什么、不判断她该不该回。「注意到」和
「开口」离得太近，一不小心 @ 就又变成回复开关——所以这条线在这里被切断：
:func:`nudge_once` 的返回值只说明"这一缝跑了没有"，跟她开没开口没有任何关系。

**同一条消息只把她叫来一次。** 她没看手机的话那条一直未读，按"还有没有未读"判就是每
分钟震一次、一天一千多次模型调用。做法不是加冷却，而是让**那一缝的身份就是那条消息**
（``nudge:<message_id>``）：跑过就是跑过了，新消息才是新的一缝。真人手机就是这样——
新消息才震，躺着的未读不会一直震。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Annotated

from app.infra.cst_time import now_cst
from app.living.clock import living_lane
from app.living.moment import LIVING_PERSONAS, LifeMoment, run_moment
from app.living.phone import newest_unread_summons
from app.runtime.data import Data, Key
from app.runtime.node import node

logger = logging.getLogger(__name__)

# 一分钟一拍。这一拍绝大多数时候只是几次读库 + 比大小，一句模型都不调；拍得密只是让
# "有人叫她"到"她被带到那一刻"之间的延迟压在一分钟内（常规缝是十分钟）。
PHONE_NUDGE_TICK_SECONDS = 60


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
    """有人在叫她就把她带到这一刻；没有、或者这条已经叫过了，返回 ``None``。

    返回值只回答"这一缝跑了没有"。**她回不回是她的输出**，不在这里判、也不该有人在
    这里判。
    """
    summons = await newest_unread_summons(lane=lane, persona_id=persona_id)
    if summons is None:
        return None
    return await run_moment(
        lane=lane,
        persona_id=persona_id,
        now=now,
        nudged_by=summons.message_id,
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
                "living nudge lane=%s persona=%s 这一缝炸了：%r",
                lane,
                persona_id,
                outcome,
                exc_info=outcome,
            )
        elif outcome is not None:
            logger.info(
                "living nudge lane=%s persona=%s 被叫来提前一缝（%s），说：%s",
                lane,
                persona_id,
                outcome.moment_id,
                outcome.said,
            )
