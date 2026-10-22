"""day_page 和 persona_review 两个插件：她的日记和她每周的回看，各有一条自己的队列。

  * day_page 插件对象（:class:`app.living.day_page.pages.Diary`）：她一轮里"上一次写下的那一天"那一段。
  * persona_review 插件：每周回看读的是 ``phone`` 服务交回的别人在手机上对她说的，和 ``received`` 服务
    交回的传到她这里的。

``persona``、``her_records``、``received``、``phone`` 服务用替身，库是用例的库。日记怎么判、怎么写在
``test_day_page.py``，回看在 ``test_persona_review.py``；这里只验插件这一面。
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from types import SimpleNamespace

import pytest

from app.agent.round import Round

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))


def _on(day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, day, hour, minute, tzinfo=_CST)


class _Unused:
    """一个这一步不该问到的服务的替身。"""


class _NoReviewYet:
    """``persona`` 服务的替身：这一周她还没回看过。"""

    async def has_review_this_week(self, *, lane, persona_id, now):
        return False


class _SaidToHerOnPhone:
    """``phone`` 服务的替身：交回给定的那几条别人对她说的，问过哪一段记下来。"""

    def __init__(self, *said) -> None:
        self.said = list(said)
        self.asked: list[tuple] = []

    async def said_to_her(self, *, persona_id, since, until):
        self.asked.append((persona_id, since, until))
        return self.said

    def said_to_her_line(self, said):
        return f"手机上的一行：{said.body}"


class _ReceivedLetters:
    """``received`` 服务的替身：交回给定的那几条传到她这里的，问过哪一段记下来。"""

    def __init__(self, *letters) -> None:
        self.letters = list(letters)
        self.asked: list[tuple] = []

    async def between(self, *, lane, persona_id, since, until):
        self.asked.append((lane, persona_id, since, until))
        return self.letters

    def line(self, letter):
        return f"收到的一行：{letter.body}"


def _diary():
    from app.living.day_page.pages import Diary

    return Diary(persona=_Unused(), her_records=_Unused(), received=_Unused())


def _round(now: dt.datetime) -> Round:
    return Round(
        app="agent-service",
        lane=LANE,
        participant="akao",
        profile="resident",
        round_id=now.isoformat(timespec="minutes"),
        now=now,
        reason="test",
        due=None,
    )


async def test_the_diary_and_the_look_back_each_have_her_queues_and_their_wakes(app_host):
    """day_page 插件登记"上一次写下的那一天"那一段，给三姐妹各开一条日记队列、
    各挂一个日记的唤醒；persona_review 插件给三姐妹各开一条回看队列、各挂一个回看的唤醒。"""
    host = await app_host("agent-service")
    registered = host.registered()

    assert Counter((r.plugin, r.kind) for r in registered if r.plugin == "day_page") == {
        ("day_page", "section"): 1,
        ("day_page", "turns"): 3,
        ("day_page", "wake"): 3,
    }
    assert Counter(
        (r.plugin, r.kind) for r in registered if r.plugin == "persona_review"
    ) == {("persona_review", "turns"): 3, ("persona_review", "wake"): 3}
    assert {r.name for r in registered if r.plugin == "day_page" and r.kind == "wake"} == {
        f"day-page -> {p}/day-page" for p in ("akao", "ayana", "chinagi")
    }
    assert {
        r.name for r in registered if r.plugin == "persona_review" and r.kind == "wake"
    } == {f"persona-review -> {p}/persona-review" for p in ("akao", "ayana", "chinagi")}
    assert {(r.kind, r.name) for r in registered if r.plugin == "day_page"} >= {
        ("section", "day_page.last_written"),
    }


@pytest.mark.integration
async def test_the_last_page_she_wrote_is_the_one_before_today(living_db):
    """"上一次写下的那一天"读的是当前生活日之前最新的一页：凌晨写下的那页写的是刚过去那一天，是昨天。"""
    from app.data.persist import insert_idempotent
    from app.living.day_page.pages import LivingDayPage

    await insert_idempotent(
        LivingDayPage(
            lane=LANE,
            persona_id="akao",
            day=dt.date(2026, 7, 24),
            text="胶片摊了一茶几。",
            written_at=_on(25, 4, 30),
            happenings=3,
        )
    )

    shown = await _diary().last_written_section(_round(_on(25, 14)))

    assert shown == "昨天（07-24）你写下的：\n胶片摊了一茶几。"


async def test_the_look_back_asks_the_phone_and_the_mailbox_for_last_full_week():
    """每周回看要回看的那一周，别人在手机上对她说的问 ``phone`` 服务，传到她这里的问 ``received`` 服务，
    问的都是上一个完整自然周（周一零点到下周一零点，CST）；摆给她的行是两个服务各自交回的样子，按时间
    先后合在一起。"""
    from app.living.persona_review.review import persona_review_due

    said = SimpleNamespace(at=_on(21, 9, 12), body="考完试了吗")
    letter = SimpleNamespace(message_time=_on(20, 19), body="一起吃晚饭吗")
    phone = _SaidToHerOnPhone(said)
    received = _ReceivedLetters(letter)

    due = await persona_review_due(
        lane=LANE,
        persona_id="akao",
        at=_on(27, 6, 30),
        clock=lambda: _on(27, 6, 30),
        persona=_NoReviewYet(),
        phone=phone,
        received=received,
    )

    week = (_on(20, 0), _on(27, 0))
    assert phone.asked == [("akao", *week)]
    assert received.asked == [(LANE, "akao", *week)]
    assert due is not None and due.evidence.heard == (
        (_on(20, 19), "【身边】收到的一行：一起吃晚饭吗"),
        (_on(21, 9, 12), "【手机】手机上的一行：考完试了吗"),
    )
