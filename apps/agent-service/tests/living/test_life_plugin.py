"""life 插件：她的一轮本身 —— 她的轮次队列和叫醒它的两个来源、她在世界里的六只手（:class:`app.living.life.moment.InPerson`
的方法）、她一轮 prompt 里的三段、"现在"那一段、落地那一步和存上下文那一步。

``residents``、``her_records`` 服务用替身。几只手各自的行为在 ``test_moment.py``、``test_look_around.py``，
两个来源在 ``test_moment.py``、``test_nudge.py``；这里只验插件这一面：登记了什么，几只手问的是哪个服务。
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass

from app.agent.round import Round, in_round
from app.agent.tooling import Tool
from app.living.services import KIND_SPEECH, MEDIUM_IN_PERSON
from tests.living.conftest import NamesRead

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))
_NOW = dt.datetime(2026, 7, 25, 14, 10, tzinfo=_CST)
_HER = ("akao", "ayana", "chinagi")


@dataclass(frozen=True)
class _Stood:
    """她当前在哪、在做什么的一条（``her_records`` 服务交回的那种）。"""

    moment_id: str
    seq: int
    place: str
    doing: str
    noted_at: dt.datetime


class _RecordsOf:
    """``her_records`` 服务的替身：她当前站在 ``stood``（``None`` 是从没落过位置）；记下的位置和经历都留在这里。"""

    def __init__(self, stood: _Stood | None) -> None:
        self.stood = stood
        self.noted: list[dict] = []
        self.happened: list[dict] = []

    async def current_whereabouts(self, *, lane, persona_id):
        assert (lane, persona_id) == (LANE, "akao")
        return self.stood

    async def note_whereabouts(self, **kwargs):
        self.noted.append(kwargs)

    async def record_happening(self, **kwargs):
        self.happened.append(kwargs)


def _round(previous_at: dt.datetime | None = None) -> Round:
    return Round(
        app="agent-service",
        lane=LANE,
        participant="akao",
        profile="resident",
        round_id="2026-07-25T14:10+08:00",
        now=_NOW,
        reason="test",
        due=None,
        previous_at=previous_at,
    )


def _in_person(her_records):
    from app.living.life.moment import InPerson

    return InPerson(residents=NamesRead(), her_records=her_records)


async def test_the_life_plugin_places_her_round_queue_its_wakes_and_what_her_round_is_made_of(
    app_host,
):
    """life 插件给三姐妹各开一条轮次队列、各挂常规节奏和提前叫醒两个唤醒；登记六只手（只有看一眼周围的
    回答当素材裁，只有到此为止结束这一轮）、她一轮 prompt 的三段、"现在"那一段、上一轮的记录、落地那一步
    和存上下文那一步。"""
    from app.living.life import PLUGIN

    host = await app_host("agent-service")
    mine = [r for r in host.registered() if r.plugin == "life"]

    assert PLUGIN.requires == ("residents", "her_records", "loose_ends", "phone", "received")
    assert Counter(r.kind for r in mine) == {
        "turns": 3,
        "wake": 6,
        "tool": 6,
        "segment": 3,
        "round_log": 1,
        "section": 1,
        "commit": 1,
        "after_commit": 1,
    }
    assert {r.name for r in mine if r.kind == "wake"} == {
        f"{source} -> {persona}/round" for source in ("regular", "nudge") for persona in _HER
    }
    assert {
        r.name: (r.detail["spec"].trim, r.detail["spec"].ends_round)
        for r in mine
        if r.kind == "tool"
    } == {
        "switch_to": ("kept", False),
        "move_to": ("kept", False),
        "say": ("kept", False),
        "act": ("kept", False),
        "look_around": ("material", False),
        "stop_for_now": ("kept", True),
    }
    assert {(r.kind, r.name) for r in mine if r.kind not in ("turns", "wake", "tool")} == {
        ("segment", "living_life_moment/this-moment"),
        ("segment", "living_life_moment/rules"),
        ("segment", "living_life_moment/voice"),
        ("round_log", "life.moments"),
        ("section", "life.now"),
        ("commit", "life.moment"),
        ("after_commit", "history"),
    }


async def test_moving_takes_what_is_in_her_hands_along_from_her_records():
    """挪个地方问 ``her_records`` 服务她手上是什么事，原样带到新地方记下，位置的键从这一轮的身份来。"""
    records = _RecordsOf(
        _Stood("2026-07-25T14:00+08:00", 1, "家/客厅", "看昨天拍的胶片", _NOW)
    )

    with in_round(_round()):
        said = await Tool(_in_person(records).move_to).invoke({"place": "家/阳台"})

    assert said == "你在 家/阳台，还在看昨天拍的胶片。"
    (noted,) = records.noted
    assert (noted["place"], noted["doing"], noted["noted_at"]) == ("家/阳台", "看昨天拍的胶片", _NOW)
    assert noted["moment_id"].startswith("2026-07-25T14:10+08:00:")


async def test_saying_something_names_a_sister_through_residents_and_records_it_as_hers():
    """当面说一句记在 ``her_records`` 服务上：说给姐妹的，收件人记成 ``residents`` 服务读好的名字。"""
    records = _RecordsOf(_Stood("2026-07-25T14:00+08:00", 1, "家/客厅", "看胶片", _NOW))

    with in_round(_round()):
        said = await Tool(_in_person(records).say).invoke(
            {"what": "布丁我吃了。", "to": ["ayana"]}
        )

    assert said == "记下了。"
    (happened,) = records.happened
    assert happened["actor"] == "akao"
    assert (happened["kind"], happened["medium"]) == (KIND_SPEECH, MEDIUM_IN_PERSON)
    assert (happened["content"], happened["audience"]) == ("布丁我吃了。", ["绫奈"])


async def test_she_cannot_act_before_she_stands_anywhere():
    """还没落过位置就做事：``her_records`` 服务说她从没定下在哪，什么都不记。"""
    records = _RecordsOf(None)

    with in_round(_round()):
        outcome = await Tool(_in_person(records).act).invoke({"what": "把窗关上"})

    assert "先用 switch_to" in str(outcome)
    assert records.happened == []


async def test_the_now_section_is_the_rounds_clock_and_the_gap_since_the_last_one():
    """"现在"那一段只用这一轮自己的『现在』和上一轮落地的时刻，不读任何东西。"""
    from app.living.life.now import now_section

    assert await now_section(_round(previous_at=_NOW - dt.timedelta(minutes=10))) == (
        "现在 2026-07-25 周六 14:10 CST，离上一次过了 10 分钟。"
    )
    assert await now_section(_round()) == "现在 2026-07-25 周六 14:10 CST。"
