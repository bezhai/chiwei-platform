"""日历：这个家的客观时刻表怎么写进去、到期怎么变成她感知得到的事。

两处幂等是这套东西的命门，各占一节：

  * **写进去**：重启 / 每一拍都重跑一次「排今天的日历」，同一个槽只能有一条。
  * **到期交付**：``Upcoming`` 的消费是**至少一次**——拿到手了但还没来得及标
    consumed 就崩，下一拍会把同一条再交出来一次。所以写 ``Happening`` 的 id 必须
    从 ``item_id`` 派生，重放落回同一行，她才不会把一件事感知两遍。

日历本身**不花模型钱**：这里一个 agent 都没有，全是时刻表 + 比大小。
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.living.calendar import (
    AMBIENT_PLACE,
    DEFAULT_DAY_SCHEDULE,
    WORLD_ACTOR,
    DaySlot,
    day_item_id,
    deliver_due,
    load_day_schedule,
    parse_schedule,
    plan_day,
)
from app.living.upcoming import list_due_upcoming, schedule_upcoming
from app.living.whereabouts import note_whereabouts

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))
_DAY = dt.date(2026, 7, 25)


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


_SCHEDULE = (
    DaySlot(key="breakfast", at=dt.time(7, 30), what="早饭做好了", place="家/餐厅"),
    DaySlot(key="lunch", at=dt.time(12, 0), what="午饭做好了", place="家/餐厅"),
    DaySlot(key="nightfall", at=dt.time(19, 0), what="天黑了", place=None),
)


async def _where(persona_id: str, place: str, moment: str = "m1") -> None:
    await note_whereabouts(
        lane=LANE,
        persona_id=persona_id,
        moment_id=moment,
        place=place,
        doing="待着",
        noted_at=_at(6),
    )


# --------------------------------------------------------------------------
# 一 · 时刻表是配置，不是散在代码里的魔法数
# --------------------------------------------------------------------------


def test_the_builtin_schedule_covers_a_whole_day():
    """内置的一份是兜底，不是唯一——但它必须真的排满一天，不然世界没东西到期。"""
    assert len(DEFAULT_DAY_SCHEDULE) >= 5
    keys = [slot.key for slot in DEFAULT_DAY_SCHEDULE]
    assert len(set(keys)) == len(keys), "槽标识重复 = 同一天两条项撞 item_id"
    ats = [slot.at for slot in DEFAULT_DAY_SCHEDULE]
    assert ats == sorted(ats), "内置时刻表要按钟点排好，读的人才看得懂"


def test_a_configured_schedule_replaces_the_builtin_one():
    raw = (
        '[{"key":"wake","at":"06:40","what":"天亮了","place":"家"},'
        '{"key":"shop","at":"21:00","what":"抹茶店关门","place":"街上/抹茶店"}]'
    )
    got = parse_schedule(raw)
    assert got == (
        DaySlot(key="wake", at=dt.time(6, 40), what="天亮了", place="家"),
        DaySlot(key="shop", at=dt.time(21, 0), what="抹茶店关门", place="街上/抹茶店"),
    )


def test_a_slot_without_a_place_is_ambient():
    got = parse_schedule('[{"key":"dark","at":"19:00","what":"天黑了"}]')
    assert got == (DaySlot(key="dark", at=dt.time(19, 0), what="天黑了", place=None),)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not json at all",
        '{"key":"wake"}',                      # 不是数组
        '[{"key":"wake","at":"下午三点","what":"天亮"}]',  # 钟点不是钟点
        '[{"at":"06:40","what":"天亮"}]',       # 没有槽标识
        '[{"key":"wake","at":"06:40"}]',        # 没有内容
    ],
)
def test_an_unusable_config_falls_back_to_the_builtin_schedule(raw):
    """配脏了不能让世界的钟停掉 —— 退回内置那份，并且吵一声（见实现的 warning）。

    这跟 ``world_daylight_coords`` 配脏就不拼日照那条不一样：那边编一个日落时刻是
    撒谎，这边退回内置作息只是回到默认值，而「今天一件会到期的事都没有」才是真的
    把实验弄死。
    """
    assert parse_schedule(raw) == DEFAULT_DAY_SCHEDULE


async def test_the_schedule_comes_from_dynamic_config(monkeypatch):
    seen: dict[str, str] = {}

    def fake_get(key: str, *, default: str = "") -> str:
        seen["key"] = key
        return '[{"key":"dark","at":"19:00","what":"天黑了"}]'

    from app.living import calendar as calendar_mod

    monkeypatch.setattr(calendar_mod.dynamic_config, "get", fake_get)
    got = await load_day_schedule()

    assert seen["key"] == calendar_mod.LIVING_DAY_SCHEDULE_KEY
    assert [slot.key for slot in got] == ["dark"]


# --------------------------------------------------------------------------
# 二 · 排今天的日历：重启 / 重复触发不能重复生成
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_planning_a_day_puts_every_still_future_slot_on_the_ledger(living_db):
    written = await plan_day(lane=LANE, now=_at(6), schedule=_SCHEDULE)

    assert written == [
        day_item_id(_DAY, "breakfast"),
        day_item_id(_DAY, "lunch"),
        day_item_id(_DAY, "nightfall"),
    ]
    due = await list_due_upcoming(lane=LANE, until=_at(23, 59))
    assert [(u.what, u.due_at) for u in due] == [
        ("早饭做好了", _at(7, 30)),
        ("午饭做好了", _at(12)),
        ("天黑了", _at(19)),
    ]


@pytest.mark.integration
async def test_planning_the_same_day_twice_does_not_duplicate(living_db):
    """每一拍都重排一次（这就是重启后的自愈方式），第二次必须是 no-op。"""
    await plan_day(lane=LANE, now=_at(6), schedule=_SCHEDULE)
    again = await plan_day(lane=LANE, now=_at(6, 30), schedule=_SCHEDULE)

    assert again == []
    due = await list_due_upcoming(lane=LANE, until=_at(23, 59))
    assert len(due) == 3


@pytest.mark.integration
async def test_a_slot_whose_moment_already_passed_is_never_scheduled(living_db):
    """中午才起的进程不该在 12:05 给她端上「早饭做好了」。"""
    written = await plan_day(lane=LANE, now=_at(12, 5), schedule=_SCHEDULE)

    assert written == [day_item_id(_DAY, "nightfall")]
    due = await list_due_upcoming(lane=LANE, until=_at(23, 59))
    assert [u.what for u in due] == ["天黑了"]


@pytest.mark.integration
async def test_replanning_after_an_item_was_consumed_does_not_resurrect_it(living_db):
    """已经发生过的早饭不许被下一拍的重排复活。"""
    await plan_day(lane=LANE, now=_at(6), schedule=_SCHEDULE)
    await deliver_due(lane=LANE, now=_at(7, 31))

    written = await plan_day(lane=LANE, now=_at(7, 32), schedule=_SCHEDULE)

    assert written == []
    due = await list_due_upcoming(lane=LANE, until=_at(23, 59))
    assert [u.what for u in due] == ["午饭做好了", "天黑了"], "早饭被重排复活了"


@pytest.mark.integration
async def test_each_day_gets_its_own_items(living_db):
    await plan_day(lane=LANE, now=_at(6), schedule=_SCHEDULE)
    tomorrow = _at(6) + dt.timedelta(days=1)
    written = await plan_day(lane=LANE, now=tomorrow, schedule=_SCHEDULE)

    assert written == [
        day_item_id(dt.date(2026, 7, 26), "breakfast"),
        day_item_id(dt.date(2026, 7, 26), "lunch"),
        day_item_id(dt.date(2026, 7, 26), "nightfall"),
    ]


@pytest.mark.integration
async def test_planning_is_lane_isolated(living_db):
    await plan_day(lane=LANE, now=_at(6), schedule=_SCHEDULE)

    assert await list_due_upcoming(lane="prod", until=_at(23, 59)) == []


# --------------------------------------------------------------------------
# 三 · 到期交付：变成一条她感知得到的 Happening
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_due_item_becomes_something_she_can_perceive(living_db):
    from app.living.happening import read_perceived_by

    await _where("akao", "家/餐厅")
    await plan_day(lane=LANE, now=_at(6), schedule=_SCHEDULE)

    happened = await deliver_due(lane=LANE, now=_at(7, 31))

    assert [h.content for h in happened] == ["早饭做好了"]
    assert happened[0].actor == WORLD_ACTOR
    assert happened[0].occurred_at == _at(7, 30), "事情发生在它该发生的那一刻"

    window = await read_perceived_by(lane=LANE, persona_id="akao")
    assert [(p.content, p.directed) for p in window.items] == [("早饭做好了", False)]


@pytest.mark.integration
async def test_an_ambient_item_reaches_everyone_in_the_house(living_db):
    """没绑地点的事（天黑）发生在整个家的范围里，屋里每个人都感知得到原话。"""
    from app.living.happening import read_perceived_by

    await _where("akao", "家/客厅")
    await _where("ayana", "家/楼上/绫奈房间")
    await _where("mio", "学校")
    await plan_day(lane=LANE, now=_at(6), schedule=_SCHEDULE)

    happened = await deliver_due(lane=LANE, now=_at(19, 1))

    nightfall = [h for h in happened if h.content == "天黑了"]
    assert [h.place for h in nightfall] == [AMBIENT_PLACE]
    for persona in ("akao", "ayana"):
        window = await read_perceived_by(lane=LANE, persona_id=persona)
        assert "天黑了" in [p.content for p in window.items], persona
    outside = await read_perceived_by(lane=LANE, persona_id="mio")
    assert "天黑了" not in [
        p.content for p in outside.items
    ], "在学校的人看不到这个家里的天黑"


@pytest.mark.integration
async def test_nothing_due_yet_delivers_nothing(living_db):
    await plan_day(lane=LANE, now=_at(6), schedule=_SCHEDULE)

    assert await deliver_due(lane=LANE, now=_at(7)) == []


@pytest.mark.integration
async def test_a_delivered_item_is_not_delivered_again(living_db):
    await plan_day(lane=LANE, now=_at(6), schedule=_SCHEDULE)

    first = await deliver_due(lane=LANE, now=_at(7, 31))
    second = await deliver_due(lane=LANE, now=_at(7, 32))

    assert [h.content for h in first] == ["早饭做好了"]
    assert second == []


@pytest.mark.integration
async def test_delivery_also_covers_items_world_wrote(living_db):
    """交付是**账上所有东西**的唯一出口，不只是日历项 —— world 排的也走这条。"""
    from app.living.happening import read_perceived_by

    await _where("akao", "家/门口")
    await schedule_upcoming(
        lane=LANE,
        item_id="world:parcel",
        what="快递送到门口",
        due_at=_at(10),
        place="家/门口",
    )

    happened = await deliver_due(lane=LANE, now=_at(10, 1))

    assert [h.content for h in happened] == ["快递送到门口"]
    window = await read_perceived_by(lane=LANE, persona_id="akao")
    assert [p.content for p in window.items] == ["快递送到门口"]


# --------------------------------------------------------------------------
# 四 · 至少一次的消费，靠 item_id 派生的幂等 id 收口
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_redelivered_item_does_not_happen_twice(living_db, monkeypatch):
    """拿到手了但没来得及标 consumed 就崩 —— 下一拍重来，她只能感知到一遍。

    这是 ``Upcoming`` 「至少一次」语义唯一的兑现处：``list_due_upcoming`` 会如实
    把没标掉的那条再交出来一次，所以幂等只能落在写 ``Happening`` 这一步。id 不从
    ``item_id`` 派生（比如随手 uuid4），这条就会红 —— 她会把同一件事感知两遍。
    """
    from app.living import calendar as calendar_mod
    from app.living.happening import read_perceived_by

    await _where("akao", "家/餐厅")
    await plan_day(lane=LANE, now=_at(6), schedule=_SCHEDULE)

    def boom(**_kwargs):
        raise RuntimeError("标 consumed 之前进程没了")

    # 只还原这一个名字，不用 monkeypatch.undo()：那会把 test_db fixture 打的
    # session 补丁一起撤掉，后面每一句 SQL 都去连本机 5432。
    real_mark = calendar_mod.mark_upcoming_consumed
    monkeypatch.setattr(calendar_mod, "mark_upcoming_consumed", boom)
    with pytest.raises(RuntimeError):
        await deliver_due(lane=LANE, now=_at(7, 31))

    # 崩在半路：事情已经发生了，但账上那条还没销
    assert [u.item_id for u in await list_due_upcoming(lane=LANE, until=_at(8))] == [
        day_item_id(_DAY, "breakfast")
    ]

    monkeypatch.setattr(calendar_mod, "mark_upcoming_consumed", real_mark)
    again = await deliver_due(lane=LANE, now=_at(7, 40))

    assert [h.content for h in again] == ["早饭做好了"]
    window = await read_perceived_by(lane=LANE, persona_id="akao")
    assert [p.content for p in window.items] == ["早饭做好了"], (
        "同一件事落了两条 Happening —— happening_id 没有从 item_id 派生"
    )
    assert await deliver_due(lane=LANE, now=_at(8)) == []
