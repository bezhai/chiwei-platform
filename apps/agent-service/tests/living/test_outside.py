"""外面的世界：她每天自然知道的那些事，不是她要伸手去查的东西。

真人早上起来就知道在下雨、知道今天不用上学、知道追的那部今晚更新。这些不该是
「查询天气」这样一次工具调用 —— 那是工程动作，不是活着。所以底料跟日历项走同一
条腿：客观事实，不花模型钱，到点变成她感知得到的一件 :class:`Happening`。

三处必须真的成立，各占一节：

  * **一天只写一条**。每一拍都照跑，重复的拍是 no-op —— 她不会把同一天的天气感知
    好几遍，外部数据源也不会被每分钟打一次。
  * **一个源挂了不连累其余**。天气查不到就不说天气，节气照常。全挂了才什么都不写。
  * **绝不编**。查不到的东西一个字都不出现在她眼前，宁可她今天不知道天气。
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.living.happening import read_perceived_by
from app.living.outside import (
    OUTSIDE_SLOT_KEY,
    look_outside,
    outside_happening_id,
)
from app.living.whereabouts import note_whereabouts

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))
_DAY = dt.date(2026, 7, 25)


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


def _sources(**overrides):
    """五个源的替身。默认全都答得上，需要哪个失败就传 None 或异常。"""
    answers = {
        "weather": {"ok": True, "city": "广州", "temp": "24", "weather": "小雨"},
        "lunar": {"ok": True, "date": "六月初一", "term": "大暑", "days_to_next": 8},
        "holiday": {"ok": True, "kind": "工作日"},
        "anime": {"ok": True, "titles": ["夏日重现", "莉可丽丝"]},
        "sun": {"ok": True, "sunrise": "05:52", "sunset": "19:14"},
    }
    answers.update(overrides)

    async def one(name):
        got = answers[name]
        if isinstance(got, Exception):
            raise got
        return got

    return one


async def _she_is_home():
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="刚醒", noted_at=_at(7),
    )


# ---------------------------------------------------------------------------
# 一天只写一条
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_what_it_is_like_outside_becomes_something_she_perceives(living_db):
    await _she_is_home()

    written = await look_outside(lane=LANE, now=_at(8), ask=_sources())

    assert written is not None, "到点了却什么都没写"
    perceived = (await read_perceived_by(lane=LANE, persona_id="akao", after_seq=0)).items
    (seen,) = [p for p in perceived if p.happening_id == outside_happening_id(_DAY)]
    assert "小雨" in seen.content
    assert "大暑" in seen.content


@pytest.mark.integration
async def test_running_the_same_day_again_changes_nothing(living_db):
    """每一拍都照跑。重复的拍不该让她再感知一遍，也不该再打一次外部数据源。"""
    await _she_is_home()

    calls: list[str] = []

    def counting():
        inner = _sources()

        async def one(name):
            calls.append(name)
            return await inner(name)

        return one

    first = await look_outside(lane=LANE, now=_at(8), ask=counting())
    after_first = len(calls)
    again = await look_outside(lane=LANE, now=_at(8, 1), ask=counting())

    assert first is not None
    assert again is None, "同一天写了第二遍"
    assert len(calls) == after_first, (
        f"第二拍又去打了外部数据源：{calls[after_first:]}"
    )

    perceived = (await read_perceived_by(lane=LANE, persona_id="akao", after_seq=0)).items
    mine = [p for p in perceived if p.happening_id == outside_happening_id(_DAY)]
    assert len(mine) == 1, f"同一天落了 {len(mine)} 行"


@pytest.mark.integration
async def test_before_the_hour_she_does_not_know_yet(living_db):
    """没到点就是没到点 —— 半夜三点不该有人告诉她今天白天什么样。"""
    await _she_is_home()
    assert await look_outside(lane=LANE, now=_at(3), ask=_sources()) is None


# ---------------------------------------------------------------------------
# 一个源挂了不连累其余
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_one_source_down_does_not_cost_her_the_rest(living_db):
    await _she_is_home()

    written = await look_outside(
        lane=LANE,
        now=_at(8),
        ask=_sources(weather={"ok": False, "reason": "未配置和风天气 API Key"}),
    )

    assert written is not None, "天气挂了就整条不写 —— 其余四样是无辜的"
    assert "大暑" in written.content
    assert "小雨" not in written.content


@pytest.mark.integration
async def test_a_source_that_blows_up_is_treated_as_silence(living_db):
    """抛异常和"查不到"是同一个下场：这一样不说，别的照说。"""
    await _she_is_home()

    written = await look_outside(
        lane=LANE, now=_at(8), ask=_sources(weather=RuntimeError("连不上"))
    )

    assert written is not None
    assert "大暑" in written.content


@pytest.mark.integration
async def test_when_nothing_answers_she_is_told_nothing(living_db):
    """全挂了就什么都不写。**绝不编** —— 编一个天气比她不知道天气糟得多。"""
    await _she_is_home()

    silent = _sources(
        weather={"ok": False, "reason": "x"},
        lunar={"ok": False, "reason": "x"},
        holiday={"ok": False, "reason": "x"},
        anime={"ok": False, "reason": "x"},
        sun={"ok": False, "reason": "x"},
    )
    assert await look_outside(lane=LANE, now=_at(8), ask=silent) is None

    perceived = (await read_perceived_by(lane=LANE, persona_id="akao", after_seq=0)).items
    assert not [p for p in perceived if p.happening_id == outside_happening_id(_DAY)]


@pytest.mark.integration
async def test_a_failed_day_is_not_written_off(living_db):
    """全挂那天不该被记成"今天看过了" —— 数据源缓过来她还能知道。"""
    await _she_is_home()

    silent = _sources(
        weather={"ok": False, "reason": "x"},
        lunar={"ok": False, "reason": "x"},
        holiday={"ok": False, "reason": "x"},
        anime={"ok": False, "reason": "x"},
        sun={"ok": False, "reason": "x"},
    )
    assert await look_outside(lane=LANE, now=_at(8), ask=silent) is None

    later = await look_outside(lane=LANE, now=_at(8, 30), ask=_sources())
    assert later is not None, "上一拍全挂就再也不看了 —— 那一整天她都是瞎的"
    assert "小雨" in later.content


# ---------------------------------------------------------------------------
# 它是世界发生的事，不是谁做的
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_nobody_did_this_the_world_did(living_db):
    """actor 是世界。写成某个 persona 的话，回声抑制会把它从那个人眼前抹掉。"""
    await note_whereabouts(
        lane=LANE, persona_id="akao", moment_id="m1", place="家/我房间",
        doing="刚醒", noted_at=_at(7),
    )
    await note_whereabouts(
        lane=LANE, persona_id="ayana", moment_id="m1", place="家/客厅",
        doing="吃早饭", noted_at=_at(7),
    )

    await look_outside(lane=LANE, now=_at(8), ask=_sources())

    for who in ("akao", "ayana"):
        perceived = (await read_perceived_by(lane=LANE, persona_id=who, after_seq=0)).items
        assert [p for p in perceived if p.happening_id == outside_happening_id(_DAY)], (
            f"{who} 感知不到今天外面什么样"
        )


def test_the_slot_key_is_stable():
    """id 从日期派生，重放落回同一行。换掉它 == 历史上每一天都会被重新感知一遍。"""
    assert outside_happening_id(_DAY) == f"{OUTSIDE_SLOT_KEY}:2026-07-25"


# ---------------------------------------------------------------------------
# 接进那条钟：日历那一拍顺手看一眼外面
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_calendar_tick_also_looks_outside(living_db, monkeypatch):
    """挂在日历那条钟上 —— 同性质（客观事实、不花模型钱），不该另起一条。

    没挂上去的症状是**静默的**：模块写好了、测试全绿，而线上她永远不知道外面什么
    样。这条用例就是为了让"忘了挂"变红。
    """
    from app.living import clock as clock_mod

    await _she_is_home()

    monkeypatch.setattr(clock_mod, "living_lane", lambda: LANE)
    monkeypatch.setattr(clock_mod, "now_cst", lambda: _at(8))
    monkeypatch.setattr(clock_mod, "ask_the_world", _sources())

    await clock_mod.calendar_tick.__wrapped__(clock_mod.CalendarTick(ts="2026-07-25T08:00:00+08:00"))

    perceived = (await read_perceived_by(lane=LANE, persona_id="akao", after_seq=0)).items
    assert [p for p in perceived if p.happening_id == outside_happening_id(_DAY)], (
        "日历那一拍没有去看外面 —— 她永远不会知道今天下不下雨"
    )
