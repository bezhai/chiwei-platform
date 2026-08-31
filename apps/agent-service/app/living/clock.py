"""世界的钟 —— 两条时间源，一条免费一条低频。

  * :class:`CalendarTick` 每分钟一拍，跑 :mod:`app.living.calendar`：把今天还没
    到点的槽补上账、把到期的东西交付出去。**一分模型钱都不花**，所以可以拍得密，
    密的好处是日历项的到期误差压在一分钟内。
  * :class:`WorldRoundTick` 每五分钟一拍，问 :mod:`app.living.world` 要不要跑一轮。
    真正的轮次间隔是业务参数（Dynamic Config，默认 60 分钟），判在节点里——**钟不
    该由配置决定跳不跳**：``Source.interval`` 的秒数在 import 时就固定了，想让间隔
    可调只能让钟拍得比最密的间隔更密，然后在节点里判"够不够久"。这一拍绝大多数时候
    读一次库就返回。

**挂时间源的 Data 必须能只用一个 ``ts`` 字段构造。** 框架源循环固定按
``data_type(ts=<iso>)`` 造 payload，多一个必填字段就是每一拍 ValidationError
**直接杀 Pod**（不是少跑一轮，是整个服务起不来）。所以这两个 tick 干净得只有
``ts``，泳道由节点自己从进程环境读（:func:`living_lane`）——这也是对的分工：
泳道是部署事实，不是钟的内容。

非 prod 泳道默认不跑时间源（``app.runtime.lane_policy``），coe 的 ConfigBundle
已经把 ``DATAFLOW_ENABLE_TIME_SOURCES`` 覆盖成 1，实验泳道开箱能跑。
"""

from __future__ import annotations

import logging
from typing import Annotated

from app.infra.cst_time import now_cst
from app.living.calendar import deliver_due, load_day_schedule, plan_day
from app.living.world import run_world_round
from app.runtime.data import Data, Key
from app.runtime.lane_policy import current_deployment_lane
from app.runtime.node import node

logger = logging.getLogger(__name__)

# 日历那一拍不花模型钱，拍得密只是让到期误差小；一分钟足够，再密没有意义
# （她那边的粒度本来就是分钟级）。
CALENDAR_TICK_SECONDS = 60

# world 那一拍只是"问一句要不要跑"，真正的间隔由 Dynamic Config 决定。五分钟是
# 这个判断的分辨率上限：把间隔配到 5 分钟以下不会真的生效。
WORLD_ROUND_TICK_SECONDS = 300


def living_lane() -> str:
    """本进程所在的泳道；prod 部署上是 ``"prod"``。

    lane 进 living 三张表的 Key 是硬约束（runtime 不给任何 Data 自动加 lane）。
    拿不到泳道时必须落到 ``"prod"`` 而不是空串：空串会开一条谁也读不到的影子轴——
    写进去的行查不出来，而她那边一片安静，什么报错都没有。
    """
    return current_deployment_lane() or "prod"


class CalendarTick(Data):
    """日历那一拍。单字段 ``ts``，见模块 docstring 里那条杀 Pod 的约定。"""

    ts: Annotated[str, Key]

    class Meta:
        transient = True


class WorldRoundTick(Data):
    """world 轮次那一拍。同上，只有 ``ts``。"""

    ts: Annotated[str, Key]

    class Meta:
        transient = True


@node
async def calendar_tick(tick: CalendarTick) -> None:
    """补今天的日历 + 把到期的东西交付出去。

    两件都幂等，所以每一拍照跑就是重启后的自愈方式，不需要"今天排过没有"的标记。
    """
    lane, now = living_lane(), now_cst()
    written = await plan_day(
        lane=lane, now=now, schedule=await load_day_schedule()
    )
    happened = await deliver_due(lane=lane, now=now)
    if written or happened:
        logger.info(
            "living calendar lane=%s 新排 %d 件、到期 %d 件",
            lane,
            len(written),
            len(happened),
        )


@node
async def world_round_tick(tick: WorldRoundTick) -> None:
    """问一句 world 要不要跑一轮；间隔没到就什么都不做。"""
    round_ = await run_world_round(lane=living_lane(), now=now_cst())
    if round_ is not None:
        logger.info(
            "living world round lane=%s 产出 %d 件，说：%s",
            round_.lane,
            round_.produced,
            round_.said,
        )
