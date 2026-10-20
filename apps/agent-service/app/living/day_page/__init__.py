"""day_page 插件：她的日记 —— 每天凌晨她把刚过去那一天自己写成一页，之后每一轮都读得到（不是机器折叠
原文，原始记录一条不动）。

  * 状态段 ``day_page.last_written``：她上一次写下的那一天
    （:meth:`app.living.day_page.pages.Diary.last_written_section`）。
  * 三姐妹各一条日记队列（``day-page``），各挂一个日记的唤醒：钟每五分钟一拍，那一拍落在 04:00–06:00
    的窗口里、那一天还没有页，就写刚过去那一天（:func:`app.living.day_page.pages.day_page_due`）。
  * 交出 ``day_pages`` 服务（:class:`app.living.services.DayPages`）：每周回看经它读一周的日记。
  * 要 ``persona``（她是谁）、``her_records``（那一天她做过、说过的）和 ``received``（传到她这里的）服务。

**日记有自己的一条队列**，跟她的轮次、每周回看各占各的，占用上限相同（:data:`app.living.common.HOLD_LIMIT`）：
写昨天那一页不会耽误她过今天。钟的每一拍都排队（``QUEUE_EACH``）：排在一次卡住的写页后面的那一拍
照样轮得到，轮到时再判还有没有要写的。一拍算不算数按它响的时刻判（窗口），写哪一页按轮到它的时刻判。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial

from app.host import Batch, Coalesce, Context, Plugin, WakeSource
from app.living.common import HOLD_LIMIT, living_lane
from app.living.day_page.pages import (
    Diary,
    DuePage,
    LivingDayPage,
    day_page_due,
    write_due_page,
)

# 五分钟一拍：窗口两小时里试 24 次；没写成的那一拍（模型超时、她一个字都没写）由下一拍再试。
DAY_PAGE_EVERY = timedelta(minutes=5)


def day_page_wake(lane: str, diary: Diary) -> WakeSource[DuePage, LivingDayPage | None]:
    """``lane`` 上她的日记：那一拍落在窗口里、那一天还没有页，就写刚过去那一天
    （:func:`app.living.day_page.pages.day_page_due`）。"""

    async def due(persona_id: str, batch: Batch, clock: Callable[[], datetime]) -> DuePage | None:
        (tick,) = batch.close()
        return await day_page_due(
            lane=lane,
            persona_id=persona_id,
            at=tick.at,
            clock=clock,
            her_records=diary.her_records,
            received=diary.received,
        )

    return WakeSource(
        "day-page", Coalesce.QUEUE_EACH, due, partial(write_due_page, persona=diary.persona)
    )


def setup(ctx: Context) -> None:
    diary = Diary(
        persona=ctx.service("persona"),
        her_records=ctx.service("her_records"),
        received=ctx.service("received"),
    )
    ctx.provide("day_pages", diary)
    ctx.section("day_page.last_written", diary.last_written_section)
    pages = day_page_wake(living_lane(), diary)
    for persona_id in ctx.participants(profile="resident"):
        written_in = ctx.turns(persona_id, "day-page", hold_limit=HOLD_LIMIT)
        ctx.wake(written_in, pages, every=DAY_PAGE_EVERY)


PLUGIN = Plugin(
    name="day_page",
    setup=setup,
    requires=("persona", "her_records", "received"),
    provides=("day_pages",),
)
