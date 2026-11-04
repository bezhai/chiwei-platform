"""她每周回头看时写下的「这阵子」—— 一小段，说她最近的处境和她跟别人这阵子怎么样，一周换一段。

**它跟「她是谁」分开存。** 「她是谁」只有 bezhai 手写（:mod:`app.living.persona.chain`），代码不往
那条链里写；每周回看（:mod:`app.living.persona_review`）写的是这一段，写进自己这张表。之前回看整篇
重写「她是谁」，结果是删掉原有的性格、写进一两天里的情节和别人造的词，而 prompt 里的规则只管得住
一部分。改成另写一小段之后，写坏了也只坏这一周，下周换掉，「她是谁」不动。

每一段带着它说的是哪一周（``week_since`` 是那一周的周一，``week_until`` 是下一周的周一，右开），和
写下它的时刻。append-only：每写一段是一个新版本，旧的留着，读最新的那一段。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from pydantic import field_validator

from app.data.persist import insert_append, select_latest
from app.data.schema import Data, Key, Version
from app.living.common import require_aware


class WeeklyNote(Data):
    """她回头看完一周时写下的「这阵子」（一段）。

    自然键 ``(lane, persona_id)``：泳道隔离、每个人一条；``version`` 让每一段都是新的一版，旧的
    留着。``written_at`` 是她写下它的时刻（带时区的 ``datetime``，跟这一边的另外几张表一样）：每周
    回看的"这一周写过没有"就看它（:meth:`app.living.persona.chain.PersonaChain.note_written_this_week`）。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    note: str  # 她写下的那一段，原文
    week_since: date  # 回看的那一周的周一
    week_until: date  # 下一周的周一（右开）
    written_at: datetime  # 她写下它的时刻
    version: Annotated[int, Version] = 0

    @field_validator("written_at")
    @classmethod
    def _aware_written_at(cls, v: datetime) -> datetime:
        # naive 落进 TIMESTAMPTZ 会被按服务器时区解释、静默偏几小时（见 app.living.common.require_aware）。
        return require_aware("written_at", v)


async def read_latest_note(*, lane: str, persona_id: str) -> WeeklyNote | None:
    """她最近写下的那一段；还没写过返回 ``None``。"最近"按 ``version`` 算。"""
    return await select_latest(WeeklyNote, {"lane": lane, "persona_id": persona_id})


async def append_note(
    *,
    lane: str,
    persona_id: str,
    note: str,
    week_since: date,
    week_until: date,
    written_at: datetime,
) -> None:
    """追加一段（``version`` 由 ``insert_append`` 定）。"""
    await insert_append(
        WeeklyNote(
            lane=lane,
            persona_id=persona_id,
            note=note,
            week_since=week_since,
            week_until=week_until,
            written_at=written_at,
        )
    )
