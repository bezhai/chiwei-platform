"""day_page 和 persona_review 两个插件：她的日记和她每周的回看，各有一条自己的队列。

  * day_page 插件对象（:class:`app.living.day_page.pages.Diary`）：她一轮里"上一次写下的那一天"那一段，
    和 ``day_pages`` 服务（每周回看读一周的日记经它读）。
  * persona_review 插件：每周回看读的是 ``day_pages`` 服务交回的那几页。

``persona``、``her_records``、``received``、``day_pages`` 服务用替身，库是用例的库。日记怎么判、怎么写在
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


class _PagesOf:
    """``day_pages`` 服务的替身：交回给定的那几页，问过哪一段记下来。"""

    def __init__(self, *pages) -> None:
        self.pages = list(pages)
        self.asked: list[tuple] = []

    async def between(self, *, lane, persona_id, since, until):
        self.asked.append((lane, persona_id, since, until))
        return self.pages


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
    """day_page 插件登记"上一次写下的那一天"那一段、交出 ``day_pages`` 服务，给三姐妹各开一条日记队列、
    各挂一个日记的唤醒；persona_review 插件给三姐妹各开一条回看队列、各挂一个回看的唤醒。"""
    host = await app_host("agent-service")
    registered = host.registered()

    assert Counter((r.plugin, r.kind) for r in registered if r.plugin == "day_page") == {
        ("day_page", "service"): 1,
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
        ("service", "day_pages"),
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


async def test_the_look_back_reads_last_week_from_the_day_pages_service():
    """每周回看要回看的那一周的几页，问的是 ``day_pages`` 服务，问的是上一个完整自然周。"""
    from app.living.persona_review.review import persona_review_due

    page = SimpleNamespace(day=dt.date(2026, 7, 21), text="周二。")
    day_pages = _PagesOf(page)

    due = await persona_review_due(
        lane=LANE,
        persona_id="akao",
        at=_on(27, 6, 30),
        clock=lambda: _on(27, 6, 30),
        persona=_NoReviewYet(),
        day_pages=day_pages,
    )

    assert due is not None and due.pages == (page,)
    assert day_pages.asked == [(LANE, "akao", dt.date(2026, 7, 20), dt.date(2026, 7, 27))]
