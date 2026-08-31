"""她此刻在做什么、在哪 —— 写入与读取。

纯 append，"当前"就是这条轴上最新的一条。位置是客观事实。

**旁听不在这里查位置。** :mod:`app.living.happening` 在**写入事件的那一刻**调
:func:`who_is_where` 拍一张快照存进事件行里，读取时不再回来问。理由见
:class:`app.living.records.Happening` 的 ``who_was_where``：读取时查最新位置会让
同一条事件在她换房间前后被裁成两个样子。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from app.data.session import get_session
from app.living.records import Whereabouts
from app.living.serial import append_in_commit_order
from app.runtime.migrator import _table_name

_TABLE = _table_name(Whereabouts)


def whereabouts_seq_lock_key(lane: str, persona_id: str) -> str:
    """这个人在这个 lane 上的 seq 轴的占用 key（每人一条轴，互不阻塞）。"""
    return f"living:seq:whereabouts:{lane}:{persona_id}"


async def note_whereabouts(
    *,
    lane: str,
    persona_id: str,
    moment_id: str,
    place: str,
    doing: str,
    noted_at: datetime,
) -> Whereabouts:
    """记下她这一缝在哪、在做什么。同一 ``moment_id`` 重放只落一行。"""
    return await append_in_commit_order(
        Whereabouts,
        stream=whereabouts_seq_lock_key(lane, persona_id),
        scope={"lane": lane, "persona_id": persona_id},
        moment_id=moment_id,
        place=place,
        doing=doing,
        noted_at=noted_at,
    )


async def current_whereabouts(
    *, lane: str, persona_id: str
) -> Whereabouts | None:
    """她当前在哪、在做什么；从没记过返回 ``None``。

    按 ``seq`` 取最新——``created_at`` 会有同刻并列，``seq`` 是这条轴上唯一确定的
    先后。查不到不是异常：定位不到她时旁听一律够不着，定向送达照送。
    """
    sql = (
        f"SELECT * FROM {_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        f"ORDER BY seq DESC LIMIT 1"
    )
    async with get_session() as s:
        result = await s.execute(
            text(sql), {"lane": lane, "persona_id": persona_id}
        )
        row = result.mappings().first()
    if row is None:
        return None
    return Whereabouts(**{k: row[k] for k in Whereabouts.model_fields})


async def who_is_where(*, lane: str) -> dict[str, str]:
    """本 lane 上每个人**此刻**在哪：``persona_id -> 位置路径``。

    每人取自己 seq 轴上最新的一条。从没记过位置的人根本不在返回值里——"不知道她
    在哪"和"她在某处"是两件事，前者对旁听等于不在场（定向送达不走这条路，所以位置
    缺失不会让一句对她说的话丢掉）。

    调用方是 :func:`app.living.happening.record_happening`，把结果原样存进事件行。
    """
    sql = (
        f"SELECT DISTINCT ON (persona_id) persona_id, place FROM {_TABLE} "
        f"WHERE lane = :lane ORDER BY persona_id, seq DESC"
    )
    async with get_session() as s:
        result = await s.execute(text(sql), {"lane": lane})
        rows = result.mappings().all()
    return {row["persona_id"]: row["place"] for row in rows}
