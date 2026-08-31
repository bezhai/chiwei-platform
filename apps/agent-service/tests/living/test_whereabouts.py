"""「她此刻在做什么、在哪」的读写契约。

纯 append，读最新一条。位置是客观事实——旁听判档拿的就是这里的 place。
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.living.whereabouts import (
    current_whereabouts,
    note_whereabouts,
    who_is_where,
)

LANE = "coe-living"
_TEN_AM = dt.datetime(2026, 7, 25, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))


async def _note(persona_id: str, moment_id: str, place: str, doing: str):
    return await note_whereabouts(
        lane=LANE,
        persona_id=persona_id,
        moment_id=moment_id,
        place=place,
        doing=doing,
        noted_at=_TEN_AM,
    )


@pytest.mark.integration
async def test_latest_note_wins(living_db):
    await _note("akao", "m1", "家/客厅", "看视觉小说")
    await _note("akao", "m2", "家/厨房", "煮抹茶")

    got = await current_whereabouts(lane=LANE, persona_id="akao")
    assert got is not None
    assert (got.place, got.doing) == ("家/厨房", "煮抹茶")


@pytest.mark.integration
async def test_history_is_kept_not_overwritten(living_db):
    """纯 append：上一缝的位置留在表里，不是被覆盖掉。"""
    first = await _note("akao", "m1", "家/客厅", "看视觉小说")
    second = await _note("akao", "m2", "家/厨房", "煮抹茶")
    assert second.seq > first.seq


@pytest.mark.integration
async def test_same_moment_id_replayed_does_not_duplicate(living_db):
    await _note("akao", "m1", "家/客厅", "看视觉小说")
    await _note("akao", "m1", "家/客厅", "看视觉小说")

    got = await current_whereabouts(lane=LANE, persona_id="akao")
    assert got is not None and got.seq == 1


@pytest.mark.integration
async def test_unknown_persona_returns_none(living_db):
    assert await current_whereabouts(lane=LANE, persona_id="nobody") is None


@pytest.mark.integration
async def test_noted_at_round_trips_as_a_real_instant(living_db):
    """时刻是时间类型，不是随便什么文本 —— 存进去什么偏移量出来就是同一刻。"""
    await _note("akao", "m1", "家/客厅", "看视觉小说")

    got = await current_whereabouts(lane=LANE, persona_id="akao")
    assert got is not None
    assert got.noted_at == _TEN_AM


@pytest.mark.integration
async def test_who_is_where_gives_everyone_s_latest_place(living_db):
    """写事件时拍的那张快照：每人各取自己最新的一条，没记过位置的人不在里面。"""
    await _note("akao", "m1", "家/客厅", "看视觉小说")
    await _note("akao", "m2", "家/厨房", "煮抹茶")
    await _note("ayana", "m1", "学校", "上课")
    await note_whereabouts(
        lane="prod",
        persona_id="mio",
        moment_id="m1",
        place="线上/别处",
        doing="线上的事",
        noted_at=_TEN_AM,
    )

    assert await who_is_where(lane=LANE) == {"akao": "家/厨房", "ayana": "学校"}


@pytest.mark.integration
async def test_personas_and_lanes_are_isolated(living_db):
    await _note("akao", "m1", "家/客厅", "看视觉小说")
    await note_whereabouts(
        lane="prod",
        persona_id="akao",
        moment_id="m1",
        place="线上/别处",
        doing="线上的事",
        noted_at=_TEN_AM,
    )

    coe = await current_whereabouts(lane=LANE, persona_id="akao")
    prod = await current_whereabouts(lane="prod", persona_id="akao")
    assert coe is not None and coe.place == "家/客厅"
    assert prod is not None and prod.place == "线上/别处"
    assert await current_whereabouts(lane=LANE, persona_id="ayana") is None
