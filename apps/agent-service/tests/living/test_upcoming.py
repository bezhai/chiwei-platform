"""「将要发生什么」的读写契约——客观日历项，按「还没被消费过」交付。

到期本身是时刻问题（日出、饭点、店关门），所以 ``due_at`` 是真正的时间类型：
坏值在写入那一刻就被挡住，而不是等到读窗口时把整批 cast 炸掉。

**消费不是时间窗推进，是「这条还没被拿走过」。** ``(after, until]`` 只在"所有项
必定提前写入"这个假设下才对；重启补种、工具重试、world 晚提交一条已经过了游标的
item，都会被时间窗永久越过。所以这里改成：交付所有"到期了且没被消费过"的项，
消费方拿走之后自己标一笔。
"""
from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from app.living.upcoming import (
    list_due_upcoming,
    mark_upcoming_consumed,
    schedule_upcoming,
)

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


async def _schedule(item_id: str, what: str, due_at, place: str | None = None):
    await schedule_upcoming(
        lane=LANE, item_id=item_id, what=what, due_at=due_at, place=place
    )


async def _consume(item_id: str, at: dt.datetime) -> bool:
    return await mark_upcoming_consumed(
        lane=LANE, item_id=item_id, consumed_at=at
    )


# --------------------------------------------------------------------------
# 一 · 到期交付
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_only_items_already_due_are_handed_out(living_db):
    """右端点闭区间：正好到点的算到期，还没到的不算。"""
    await _schedule("a", "天亮", _at(5))
    await _schedule("b", "早饭", _at(7, 30))
    await _schedule("c", "晚饭", _at(18))

    got = await list_due_upcoming(lane=LANE, until=_at(7, 30))
    assert [u.item_id for u in got] == ["a", "b"]


@pytest.mark.integration
async def test_results_are_ordered_by_due_at(living_db):
    await _schedule("late", "店关门", _at(21))
    await _schedule("early", "快递到", _at(9))

    got = await list_due_upcoming(lane=LANE, until=_at(23))
    assert [u.item_id for u in got] == ["early", "late"]


@pytest.mark.integration
async def test_offsets_compare_by_real_instant(living_db):
    """写进去的时刻带什么偏移量都行，比的是真实时刻。"""
    await _schedule("utc", "半夜的事", dt.datetime(2026, 7, 24, 17, 0, tzinfo=dt.UTC))

    assert [u.item_id for u in await list_due_upcoming(lane=LANE, until=_at(0, 30))] == []
    assert [u.item_id for u in await list_due_upcoming(lane=LANE, until=_at(2))] == [
        "utc"
    ]


@pytest.mark.integration
async def test_place_is_optional_and_round_trips(living_db):
    await _schedule("shop", "抹茶店关门", _at(21), place="街上/抹茶店")
    await _schedule("dark", "天黑", _at(19))

    got = await list_due_upcoming(lane=LANE, until=_at(23))
    assert [(u.item_id, u.place) for u in got] == [
        ("dark", None),
        ("shop", "街上/抹茶店"),
    ]


@pytest.mark.integration
async def test_replayed_item_id_does_not_duplicate(living_db):
    await _schedule("a", "天亮", _at(5))
    await _schedule("a", "天亮", _at(5))

    got = await list_due_upcoming(lane=LANE, until=_at(23))
    assert [u.item_id for u in got] == ["a"]


@pytest.mark.integration
async def test_lane_isolation(living_db):
    await _schedule("a", "天亮", _at(5))
    await schedule_upcoming(
        lane="prod", item_id="p", what="线上的事", due_at=_at(5), place=None
    )

    got = await list_due_upcoming(lane=LANE, until=_at(23))
    assert [u.item_id for u in got] == ["a"]


# --------------------------------------------------------------------------
# 二 · due_at 是真正的时间类型
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_due_at_that_is_not_a_time_is_rejected_at_write(living_db):
    """坏值在写入那一刻就被挡住。

    ``due_at`` 是任意 TEXT 的时候，"下午三点"能顺利落库，然后**整个窗口**的 cast
    一起失败——一条脏数据让她那一缝什么日历项都读不到。而且类型是 additive-only
    的，将来改不回来，所以只能现在挡。
    """
    with pytest.raises(ValidationError):
        await _schedule("junk", "说不清什么时候", "下午三点")

    await _schedule("ok", "天亮", _at(5))
    got = await list_due_upcoming(lane=LANE, until=_at(23))
    assert [u.item_id for u in got] == ["ok"]


# --------------------------------------------------------------------------
# 三 · 消费按「还没被拿走过」，扛得住晚提交
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_consumed_item_is_not_handed_out_again(living_db):
    await _schedule("a", "天亮", _at(5))
    assert [u.item_id for u in await list_due_upcoming(lane=LANE, until=_at(6))] == [
        "a"
    ]

    assert await _consume("a", _at(6)) is True
    assert await list_due_upcoming(lane=LANE, until=_at(23)) == []


@pytest.mark.integration
async def test_marking_the_same_item_twice_is_a_no_op(living_db):
    """durable 重投 / 工具重试会重来一遍，第二次不许改写第一次的消费时刻。"""
    await _schedule("a", "天亮", _at(5))
    assert await _consume("a", _at(6)) is True
    assert await _consume("a", _at(9)) is False
    assert await list_due_upcoming(lane=LANE, until=_at(23)) == []


@pytest.mark.integration
async def test_an_overdue_item_written_late_is_still_handed_out(living_db):
    """晚提交且 due_at 已经过期的项不许丢。

    这是 ``(after, until]`` 开窗真正会出事的地方：消费方已经把游标推过 08:00，
    world 现在才补写一条 07:30 到期的 item——按时间窗它永远不会再被读到。重启补种、
    重试、world 晚一步提交都会走到这里，不是小概率。
    """
    await _schedule("early-bird", "闹钟", _at(7))
    first = await list_due_upcoming(lane=LANE, until=_at(8))
    assert [u.item_id for u in first] == ["early-bird"]
    await _consume("early-bird", _at(8))

    # 游标早就过去了，这条现在才写进来
    await _schedule("late-comer", "快递到", _at(7, 30))

    second = await list_due_upcoming(lane=LANE, until=_at(8, 10))
    assert [u.item_id for u in second] == ["late-comer"]


@pytest.mark.integration
async def test_an_unconsumed_item_keeps_coming_back(living_db):
    """消费方崩在半路（拿了但没标）——下一缝还要能再拿到，不是丢掉。"""
    await _schedule("a", "天亮", _at(5))

    for _ in range(3):
        got = await list_due_upcoming(lane=LANE, until=_at(23))
        assert [u.item_id for u in got] == ["a"]


@pytest.mark.integration
async def test_marking_something_that_was_never_scheduled_raises(living_db):
    with pytest.raises(LookupError):
        await _consume("never-existed", _at(6))


@pytest.mark.integration
async def test_a_naive_consumed_at_is_rejected(living_db):
    """``model_copy(update=...)`` 不走校验——这条路必须单独堵。

    ``consumed_at`` 是版本链上那一版的内容，走的不是构造函数而是
    ``model_copy``，pydantic v2 在那条路上完全不 validate。挡了 ``due_at``
    却漏了它，就是"挡一半"。
    """
    await _schedule("a", "天亮", _at(5))

    with pytest.raises(ValidationError, match="时区"):
        await mark_upcoming_consumed(
            lane=LANE, item_id="a", consumed_at=dt.datetime(2026, 7, 25, 6, 0)
        )

    # 没被标掉，下一缝还拿得到
    assert [u.item_id for u in await list_due_upcoming(lane=LANE, until=_at(6))] == [
        "a"
    ]
