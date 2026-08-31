"""她惦记着没了结的事 —— 状态快照里唯一由她自己维护的那一层。

这一层是整个 T2 的命门：滚动窗口会把东西滚出去，清单是她**主动从窗口里救出来**
的东西。所以三件事必须真的成立：

  * 她列出来的东西会一直跟着她，跨多少缝都不掉；
  * 每一条能指出**是从哪一缝带过来的**（``opened_moment_id`` 第一次写下就不再变）；
  * 她下一次不再列出来的，就是关掉了 —— 关掉是**她的省略**在生效，不是代码替她
    判断"这条过期了"。
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.living.loose_ends import (
    LooseEnd,
    derive_thread_id,
    list_open_loose_ends,
    rewrite_loose_ends,
)

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


def _moment(hour: int, minute: int = 0) -> str:
    return _at(hour, minute).isoformat(timespec="minutes")


@pytest.fixture
async def ends_db(living_db):
    from tests.runtime.conftest import migrate

    await migrate(LooseEnd, living_db)
    return living_db


async def _rewrite(*items: str, at: dt.datetime, persona: str = "akao"):
    return await rewrite_loose_ends(
        lane=LANE,
        persona_id=persona,
        moment_id=at.isoformat(timespec="minutes"),
        now=at,
        still_on_my_mind=items,
    )


# --------------------------------------------------------------------------
# 一 · 写下来的东西跟着她走
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_something_she_listed_stays_on_her_mind(ends_db):
    await _rewrite("周末陪绫奈去祭典", at=_at(14))

    open_now = await list_open_loose_ends(lane=LANE, persona_id="akao")
    assert [e.what for e in open_now] == ["周末陪绫奈去祭典"]
    assert open_now[0].closed_at is None


@pytest.mark.integration
async def test_a_thing_carried_across_many_moments_names_the_moment_it_came_from(
    ends_db,
):
    """验收正条：跨多缝没被遗忘，而且指得出是从哪一缝带过来的。"""
    await _rewrite("周末陪绫奈去祭典", at=_at(14, 0))
    for minute in (10, 20, 30, 40, 50):
        await _rewrite("周末陪绫奈去祭典", at=_at(14, minute))

    open_now = await list_open_loose_ends(lane=LANE, persona_id="akao")
    assert len(open_now) == 1
    assert open_now[0].opened_moment_id == _moment(14, 0), (
        "重写清单把「从哪一缝带过来的」冲掉了 —— 这条一丢，跨缝延续就没有证据了"
    )
    assert open_now[0].opened_at == _at(14, 0)


@pytest.mark.integration
async def test_listing_it_again_does_not_pile_up_duplicates(ends_db):
    await _rewrite("洗的衣服还在阳台", at=_at(12))
    await _rewrite("洗的衣服还在阳台", at=_at(13))

    assert len(await list_open_loose_ends(lane=LANE, persona_id="akao")) == 1


# --------------------------------------------------------------------------
# 二 · 她不再列出来的，就是关掉了
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_leaving_it_out_next_time_closes_it(ends_db):
    await _rewrite("洗的衣服还在阳台", "周末陪绫奈去祭典", at=_at(12))

    await _rewrite("周末陪绫奈去祭典", at=_at(13))

    assert [e.what for e in await list_open_loose_ends(lane=LANE, persona_id="akao")] == [
        "周末陪绫奈去祭典"
    ]


@pytest.mark.integration
async def test_an_empty_list_closes_everything(ends_db):
    """一件都不列 = 她说心里空了。代码不许自作主张替她留着。"""
    await _rewrite("洗的衣服还在阳台", "周末陪绫奈去祭典", at=_at(12))

    await _rewrite(at=_at(13))

    assert await list_open_loose_ends(lane=LANE, persona_id="akao") == []


@pytest.mark.integration
async def test_a_closed_thing_records_which_moment_closed_it(ends_db):
    await _rewrite("洗的衣服还在阳台", at=_at(12))
    await _rewrite(at=_at(13))

    from app.runtime.persist import select_latest

    latest = await select_latest(
        LooseEnd,
        {
            "lane": LANE,
            "persona_id": "akao",
            "thread_id": derive_thread_id("洗的衣服还在阳台"),
        },
    )
    assert latest.closed_at == _at(13)
    assert latest.closed_moment_id == _moment(13)


@pytest.mark.integration
async def test_picking_a_closed_thing_back_up_keeps_its_original_moment(ends_db):
    """她又惦记起来 = 这件事从最早那一缝起就一直在她心上，不是今天新长出来的。"""
    await _rewrite("洗的衣服还在阳台", at=_at(12))
    await _rewrite(at=_at(13))

    await _rewrite("洗的衣服还在阳台", at=_at(14))

    open_now = await list_open_loose_ends(lane=LANE, persona_id="akao")
    assert [e.what for e in open_now] == ["洗的衣服还在阳台"]
    assert open_now[0].opened_moment_id == _moment(12)
    assert open_now[0].closed_at is None


# --------------------------------------------------------------------------
# 三 · 边界：空白、重复、隔离
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_blank_entries_are_not_things_on_her_mind(ends_db):
    await _rewrite("  ", "", "周末陪绫奈去祭典", at=_at(14))

    assert [e.what for e in await list_open_loose_ends(lane=LANE, persona_id="akao")] == [
        "周末陪绫奈去祭典"
    ]


@pytest.mark.integration
async def test_the_same_thing_said_twice_in_one_list_is_one_thing(ends_db):
    await _rewrite("周末陪绫奈去祭典", " 周末陪绫奈去祭典 ", at=_at(14))

    assert len(await list_open_loose_ends(lane=LANE, persona_id="akao")) == 1


@pytest.mark.integration
async def test_one_sisters_mind_is_not_another_sisters(ends_db):
    await _rewrite("周末陪绫奈去祭典", at=_at(14), persona="akao")
    await _rewrite(at=_at(14, 1), persona="ayana")

    assert len(await list_open_loose_ends(lane=LANE, persona_id="akao")) == 1
    assert await list_open_loose_ends(lane=LANE, persona_id="ayana") == []


@pytest.mark.integration
async def test_another_lane_is_another_world(ends_db):
    await rewrite_loose_ends(
        lane="prod",
        persona_id="akao",
        moment_id=_moment(14),
        now=_at(14),
        still_on_my_mind=["线上那件事"],
    )

    assert await list_open_loose_ends(lane=LANE, persona_id="akao") == []


@pytest.mark.integration
async def test_rewrite_hands_back_what_is_open_now(ends_db):
    """调用方（一缝）要能立刻报出"缝末还挂着几件"，不用再查一次库。"""
    got = await _rewrite("洗的衣服还在阳台", "周末陪绫奈去祭典", at=_at(12))

    assert sorted(e.what for e in got) == sorted(
        ["洗的衣服还在阳台", "周末陪绫奈去祭典"]
    )
