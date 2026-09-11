"""外面的世界 —— 她每天自然知道的那些事。

天气、今天是不是要上学、什么节气、今晚有什么番更新、天几点黑。真人早上起来就知道
在下雨，不需要"查询天气"这个动作。**所以这些不是一只手。** 给她一只 `query_weather`
等于要求她想得起来去问，而她想不起来的那些日子里，外面就不存在 —— 那不是活着，是
在等一个 API 被调用。

跟 :mod:`app.living.calendar` 同一条腿：客观事实、不花一分钱模型费、到点变成一件
:class:`~app.living.records.Happening`。区别只在**这个家里面**和**这个家外面**。

**一天只看一次，看到了就不再看。** 幂等落在 ``happening_id`` 上（:func:`outside_happening_id`
从日期派生），而且在打任何外部数据源**之前**先查库 —— 靠 ``record_happening`` 的
重放幂等只能防住"她感知两遍"，防不住"每分钟打五次外部 API"。

**一个源挂了不连累其余，全挂了什么都不写。** 天气查不到就不说天气，节气照常说；
五样全哑就当今天没看成，**下一拍还会再看**（不写空行占位）。**绝不编** —— 编一个
天气比她今天不知道天气糟得多，因为她会拿编的那个去跟人说话。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import text

from app.data.session import get_session
from app.infra.cst_time import CST
from app.living.happening import record_happening
from app.living.records import (
    AMBIENT_PLACE,
    KIND_ACT,
    MEDIUM_IN_PERSON,
    WORLD_ACTOR,
    Happening,
)
from app.runtime.migrator import _table_name

logger = logging.getLogger(__name__)

# 她开始知道今天外面什么样的那一刻。跟作息表里"家里开始有人起床走动"同一档 ——
# 早于这个点她还在睡，晚于这个点她已经出过房间门了。
OUTSIDE_AT = time(8, 0)

# ``happening_id`` 的前缀。**换掉它 == 历史上每一天都会被重新感知一遍**（旧的那些
# id 再也不会被查到，于是每天都像是第一次看）。
OUTSIDE_SLOT_KEY = "outside"

_TABLE = _table_name(Happening)

# 五样各自怎么说成人话。顺序就是她读到的顺序：先是抬头能看见的（天气、天色），再是
# 日子本身（节气、上不上学），最后是她自己关心的（番）。
_SAYERS: tuple[tuple[str, Callable[[dict[str, Any]], str | None]], ...] = ()


def outside_happening_id(day: date) -> str:
    """今天"外面什么样"那件事的 id —— 从日期派生，这是幂等的命门。"""
    return f"{OUTSIDE_SLOT_KEY}:{day.isoformat()}"


def _weather(got: dict[str, Any]) -> str | None:
    what = got.get("weather")
    temp = got.get("temp")
    if not what:
        return None
    if temp:
        return f"外面{what}，{temp}度"
    return f"外面{what}"


def _sun(got: dict[str, Any]) -> str | None:
    sunset = got.get("sunset")
    return f"天{sunset}黑" if sunset else None


def _lunar(got: dict[str, Any]) -> str | None:
    term = got.get("term")
    return f"今天{term}" if term else None


def _holiday(got: dict[str, Any]) -> str | None:
    kind = got.get("kind")
    return f"今天是{kind}" if kind else None


def _anime(got: dict[str, Any]) -> str | None:
    titles = got.get("titles")
    if not titles:
        return None
    named = "、".join(str(t) for t in titles[:3])
    return f"今天更新：{named}"


_SAYERS = (
    ("weather", _weather),
    ("sun", _sun),
    ("lunar", _lunar),
    ("holiday", _holiday),
    ("anime", _anime),
)


async def _already_looked(*, lane: str, day: date) -> bool:
    """今天看过没有。**在打外部数据源之前问**，否则每一拍都会打一轮再发现是重复。"""
    sql = f"SELECT 1 FROM {_TABLE} WHERE lane = :lane AND happening_id = :hid LIMIT 1"
    async with get_session() as s:
        row = (
            await s.execute(
                text(sql), {"lane": lane, "hid": outside_happening_id(day)}
            )
        ).first()
    return row is not None


async def _ask_one(
    ask: Callable[[str], Awaitable[dict[str, Any]]], name: str
) -> dict[str, Any] | None:
    """问一样。**挂了跟"查不到"是同一个下场**：这一样不说，别的照说。"""
    try:
        got = await ask(name)
    except Exception as exc:  # noqa: BLE001 — 一个源炸了不该连累其余四样
        logger.warning("living outside %s 没答上来：%s", name, exc)
        return None
    if not isinstance(got, dict) or not got.get("ok"):
        reason = got.get("reason") if isinstance(got, dict) else got
        logger.info("living outside %s 没答上来：%s", name, reason)
        return None
    return got


async def ask_the_world(name: str) -> dict[str, Any]:
    """真的去问那五个源。**只有这一个函数出网**，别处一律注 ``ask``。

    那五个查询是 :mod:`app.agent.tools.external_sources` 里现成的（各自带 800 行
    单测），本文件只负责挑哪几样、按什么顺序说给她听。它们是 ``@tool``，所以走
    ``.invoke({})`` —— 这里没有模型，只是复用它们的取数和降级。
    """
    from app.agent.tools.external_sources import (
        query_anime_calendar,
        query_holiday,
        query_lunar_term,
        query_sun_times,
        query_weather,
    )

    which = {
        "weather": query_weather,
        "sun": query_sun_times,
        "lunar": query_lunar_term,
        "holiday": query_holiday,
        "anime": query_anime_calendar,
    }[name]
    got = await which.invoke({})
    return got if isinstance(got, dict) else {"ok": False, "reason": str(got)}


async def look_outside(
    *,
    lane: str,
    now: datetime,
    ask: Callable[[str], Awaitable[dict[str, Any]]],
) -> Happening | None:
    """到点了就看一眼外面，写成她感知得到的一件事；没到点 / 今天看过了返回 ``None``。

    ``ask`` 收一个源的名字（weather / sun / lunar / holiday / anime），返回那五个
    查询各自的结构化结果。注进来而不是直接 import，是因为**这一步要打真网络**：
    测试里不该有任何一次真实出网，而"打没打"本身是要被断言的事
    （``tests/living/test_outside.py`` 有一条钉着重复的拍不许再打）。

    返回**这一天那条**记录。全挂那天不写空行占位 —— 那等于把这一天记成"看过了"，
    数据源缓过来她也再不会知道。
    """
    today = now.astimezone(CST).date()
    if now.astimezone(CST).timetz().replace(tzinfo=None) < OUTSIDE_AT:
        return None
    if await _already_looked(lane=lane, day=today):
        return None

    answers = await asyncio.gather(
        *(_ask_one(ask, name) for name, _ in _SAYERS)
    )
    lines = [
        said
        for (_, say), got in zip(_SAYERS, answers, strict=True)
        if got is not None and (said := say(got)) is not None
    ]
    if not lines:
        # 五样全哑。什么都不写 —— 下一拍还会再看。
        logger.warning("living outside lane=%s %s 五样全没答上来", lane, today)
        return None

    return await record_happening(
        lane=lane,
        happening_id=outside_happening_id(today),
        # 世界自己的事，不是谁做的。写成某个 persona 的话，回声抑制
        # （``perceive`` 里 ``actor == persona_id`` 那条）会把它从那个人眼前抹掉。
        actor=WORLD_ACTOR,
        # 没绑地点：外面什么样是这一整片上的事，屋里每个人都在这片里面。
        place=AMBIENT_PLACE,
        kind=KIND_ACT,
        medium=MEDIUM_IN_PERSON,
        content="，".join(lines),
        occurred_at=now,
    )
