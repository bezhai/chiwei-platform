"""她此刻在做什么、在哪 —— 写入与读取。

纯 append，"当前"就是这条轴上最新的一条。位置是客观事实。

**只给她自己读。** 每一条查询都按 ``persona_id`` 筛到一个人
（``tests/living/test_only_her_own_records.py`` 守着）。别人在哪，world 按各人自己报的
位置判断（:mod:`app.living.household.outgoing` 的汇总末尾、:meth:`app.living.life.moment.InPerson.look_around`
的提问里都写着她在哪），life 不读。
"""

from __future__ import annotations

from datetime import datetime

from app.data.ordered import insert_in_commit_order
from app.data.persist import fetch_one, from_row
from app.data.schema import table_name
from app.living.her_records.records import Whereabouts

_TABLE = table_name(Whereabouts)


async def note_whereabouts(
    *,
    lane: str,
    persona_id: str,
    moment_id: str,
    place: str,
    doing: str,
    noted_at: datetime,
) -> Whereabouts:
    """记下她这一轮在哪、在做什么，拿到它在她自己那条轴上的号。

    同一 ``moment_id`` 重放只落一行，返回库里已有的那一行。
    """
    return await insert_in_commit_order(
        Whereabouts(
            lane=lane,
            persona_id=persona_id,
            moment_id=moment_id,
            place=place,
            doing=doing,
            noted_at=noted_at,
        )
    )


async def current_whereabouts(
    *, lane: str, persona_id: str
) -> Whereabouts | None:
    """她当前在哪、在做什么；从没记过返回 ``None``。

    按 ``seq`` 取最新——``created_at`` 会有同刻并列，``seq`` 是这条轴上唯一确定的
    先后。查不到不是异常：她还没定下自己在哪，调用方各自如实说（"手上"那一句、
    环顾四周、``say`` / ``act`` 拒绝）。
    """
    sql = (
        f"SELECT * FROM {_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        f"ORDER BY seq DESC LIMIT 1"
    )
    row = await fetch_one(sql, lane=lane, persona_id=persona_id)
    return None if row is None else from_row(Whereabouts, row)
