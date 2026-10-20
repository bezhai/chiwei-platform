"""her_records 插件：她自己的经历（做过的事、说过的话）和她在哪 —— 只记她自己的，也只给她自己读。

  * 状态段 ``her_records.recent``：她刚做过、说过的那几条；输入段 ``her_records.whereabouts``：她在哪、
    在做什么（:mod:`app.living.her_records.book`）。
  * 交出 ``her_records`` 服务（:class:`app.living.services.HerRecords`，实现是
    :class:`app.living.her_records.book.OwnRecords`）：别的功能经它记下和读回她的经历和位置。
  * 要 ``residents`` 服务：她记下的"说给谁"里，姐妹的 id 摆出来是姐妹的名字。

两张表的形态见 :mod:`app.living.her_records.records`，经历怎么写、怎么读、摆出来什么样见
:mod:`app.living.her_records.deeds`，位置见 :mod:`app.living.her_records.whereabouts`。
"""
from __future__ import annotations

from app.host import Context, Plugin
from app.living.her_records.book import OwnRecords


def setup(ctx: Context) -> None:
    records = OwnRecords(residents=ctx.service("residents"))
    ctx.provide("her_records", records)
    ctx.section("her_records.recent", records.recent_section)
    ctx.section("her_records.whereabouts", records.whereabouts_section)


PLUGIN = Plugin(
    name="her_records",
    setup=setup,
    requires=("residents",),
    provides=("her_records",),
)
