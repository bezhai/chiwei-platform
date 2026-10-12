"""她自己的经历怎么落库。

两件必须成立的事都在这个文件里：
  * 落库按提交顺序取号，三个人共用本 lane 一条轴：并发写不撞号、不留洞，重放只落一行。
    "可见的 ``seq`` 永远是一段连续前缀"这条保证本身（跨连接、跨进程）守在
    ``tests/data/test_ordered.py``，这里验的是她的经历确实走那条路落库
  * ``kind`` / ``medium`` 是机制层硬定的枚举，写错当场炸

谁读得到这些记录（只有她自己）守在 ``tests/living/test_only_her_own_records.py``。
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.data.session import get_session
from app.living.happening import record_happening
from app.living.records import MEDIUM_IN_PERSON

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))
_TEN_AM = dt.datetime(2026, 7, 25, 10, 0, tzinfo=_CST)


async def _say(
    happening_id: str,
    *,
    actor: str = "akao",
    content: str = "周末一起去祭典吧",
    medium: str = MEDIUM_IN_PERSON,
    occurred_at: dt.datetime = _TEN_AM,
):
    return await record_happening(
        lane=LANE,
        happening_id=happening_id,
        actor=actor,
        kind="speech",
        content=content,
        medium=medium,
        occurred_at=occurred_at,
    )


async def _visible() -> list[str]:
    """此刻库里看得见的那几条，按提交序。"""
    async with get_session() as s:
        result = await s.execute(
            text(
                "SELECT happening_id FROM data_happening WHERE lane = :lane "
                "ORDER BY seq"
            ),
            {"lane": LANE},
        )
        return [row[0] for row in result.all()]


# --------------------------------------------------------------------------
# 一 · 按提交顺序取号
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_concurrent_appends_produce_a_contiguous_seq_prefix(living_db):
    """并发写不会撞号、不会留洞——可见的 seq 永远是一段连续前缀。

    这是「按提交序往后讲不漏」的根据：若能出现「seq 7 已可见、seq 6 还在飞」，
    讲到 7 之后 6 就永久丢了。
    """
    rows = await asyncio.gather(
        *(_say(f"h{i}", actor=("akao", "ayana")[i % 2]) for i in range(5))
    )
    assert sorted(r.seq for r in rows) == [1, 2, 3, 4, 5]


@pytest.mark.integration
async def test_replaying_the_same_happening_id_does_not_duplicate(living_db):
    """durable 重投 / 工具重试用同一 happening_id 再写一次——只落一行，返回第一次那行。"""
    first = await _say("h1")
    replayed = await _say("h1")

    assert replayed.seq == first.seq
    assert await _visible() == ["h1"]


# --------------------------------------------------------------------------
# 二 · kind / medium 是机制层硬定的枚举，不是自由字符串
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_unknown_medium_is_rejected_at_write(living_db):
    """``"in-person"`` 这种手滑必须当场炸。

    它落库之后的表现是静默的：``medium != MEDIUM_IN_PERSON`` 这一支会把它当成
    "隔着设备"，她当面做的事从此一件都不进 world 的汇总，而日志里什么都没有。
    """
    with pytest.raises(ValidationError):
        await _say("h1", medium="in-person")


@pytest.mark.integration
async def test_an_unknown_kind_is_rejected_at_write(living_db):
    with pytest.raises(ValidationError):
        await record_happening(
            lane=LANE,
            happening_id="h1",
            actor="akao",
            kind="thought",
            medium=MEDIUM_IN_PERSON,
            content="想了点事",
            occurred_at=_TEN_AM,
        )
