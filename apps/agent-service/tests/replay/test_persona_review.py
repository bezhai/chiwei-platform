"""Replay: her weekly review, driven the way the review clock drives it (``persona_review_tick``).

The tick runs the three sisters concurrently. Only 赤尾 has day pages in the week being read,
so the other two return before any write or model call and the order of effects is fixed.

Her day pages are seeded with the product's own write (``insert_idempotent``), between steps, so
they are not part of the record: the round under test reads them.

* ``two_weeks`` — a tick before the Monday 06:00–08:00 window does nothing. The first tick in the
  window finds her chain empty, seeds it with the persona row's text (``source='seed'``), then
  she rewrites it from last week's pages (only Monday to Sunday of that week, oldest first) with
  the flat persona text as the anchor; a later tick finds this week's review written. The next
  Monday's review reads the version she wrote the week before.
* ``model_call_fails`` — the model call fails after the chain was seeded; the tick swallows it.
  The next tick does not seed again and writes the review.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from app.capabilities._errors import CapabilityTimeout
from app.infra.cst_time import CST, now_cst
from app.living.day_page import LivingDayPage
from app.living.persona_review import PersonaReviewTick, persona_review_tick
from app.runtime.persist import insert_idempotent
from tests.replay import seeds
from tests.replay.harness import Fail, Reply

pytestmark = pytest.mark.integration

REVIEW = "living_persona_review"


def _at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=CST)


def _tick():
    return lambda: persona_review_tick(PersonaReviewTick(ts=now_cst().isoformat()))


async def _her_household_is_up(replay) -> None:
    await seeds.seed_household()
    await replay.start("agent-service")


async def _her_pages(replay, pages: dict[date, str]) -> None:
    """Day pages she wrote, as the day-page round stores them (written at 04:00 the next day)."""
    for day, words in pages.items():
        await insert_idempotent(
            LivingDayPage(
                lane=replay.lane,
                persona_id="akao",
                day=day,
                text=words,
                written_at=datetime.combine(
                    day + timedelta(days=1), time(4), tzinfo=CST
                ),
                happenings=3,
            )
        )


# The week 07-20 (Mon) .. 07-26 (Sun), with one page from the week before and the Sunday page
# out of order, to show the window and the order she reads them in.
LAST_WEEK = {
    date(2026, 7, 19): "上周日：整理了一下午的周边。",
    date(2026, 7, 26): "周日：把冲好的胶片扫进电脑，千凪借走了相机。",
    date(2026, 7, 21): "周二：第一次自己冲胶卷，手忙脚乱，但冲出来了。",
    date(2026, 7, 23): "周四：在论坛上写了一篇角色分析，有人回帖说写得好。",
}

WEEK_AFTER = {
    date(2026, 7, 28): "周二：又冲了一卷，这次没有手忙脚乱。",
    date(2026, 8, 1): "周六：和绫奈去了抹茶店，聊了很久。",
}

FIRST_REVIEW = (
    "她拍胶片、写角色分析、逛论坛、收周边、泡抹茶店。这阵子开始自己冲胶卷，"
    "第一次手忙脚乱，但喜欢上了暗房里慢慢等显影的感觉。"
)


async def test_two_weeks(replay):
    await _her_household_is_up(replay)
    await _her_pages(replay, LAST_WEEK)

    await replay.step(
        "a tick before the window does nothing", _tick(), at=_at(7, 27, 5, 55)
    )

    replay.model.script(REVIEW, Reply(text=FIRST_REVIEW))
    await replay.step(
        "the first tick in the window: the chain is seeded, then she rewrites it",
        _tick(),
        at=_at(7, 27, 6),
    )

    await replay.step(
        "a later tick finds this week's review written", _tick(), at=_at(7, 27, 6, 5)
    )

    await _her_pages(replay, WEEK_AFTER)
    replay.model.script(
        REVIEW,
        Reply(text=FIRST_REVIEW + "冲胶卷已经顺手了。跟绫奈待在一起的时间比以前多。"),
    )
    await replay.step(
        "the next Monday: she rewrites the version she wrote last week",
        _tick(),
        at=_at(8, 3, 6),
    )

    replay.check("persona_review/two_weeks")


async def test_model_call_fails(replay):
    await _her_household_is_up(replay)
    await _her_pages(replay, LAST_WEEK)

    replay.model.script(
        REVIEW,
        Fail(lambda: CapabilityTimeout("offline-model gave no answer within 180s")),
    )
    await replay.step(
        "the chain is seeded, then the model call fails; the tick swallows it",
        _tick(),
        at=_at(7, 27, 6),
    )

    replay.model.script(REVIEW, Reply(text=FIRST_REVIEW))
    await replay.step(
        "the next tick writes the review without seeding again",
        _tick(),
        at=_at(7, 27, 6, 5),
    )

    replay.check("persona_review/model_call_fails")
