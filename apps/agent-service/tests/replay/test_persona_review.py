"""Replay: her weekly review, driven the way the host's clock drives it (one tick of the
``persona-review`` wake, signalling the three sisters' review queues at once).

The tick reaches the three sisters concurrently. Only 赤尾 has a bot, and only she has anything said
to her in the week being read, so the other two return before any write or model call and the
order of effects is fixed.

What was said to her is seeded with the product's own writes, between steps, so it is not part of
the record: the round under test reads it. Messages delivered to her inbox (a sister in person,
what world tells her she noticed) are stored as her inbox stores them (``insert_idempotent`` of a
``ReceivedMessage``); messages on her phone are what lark-service would have stored in
``common_message`` (``seeds.bezhai_says``).

* ``two_weeks`` — a tick before the Monday 06:00–08:00 window does nothing. The first tick in the
  window gives her who she is (the persona row's text, her chain being empty) and what was said to
  her last week (only Monday 00:00 to the next Monday 00:00, phone and inbox merged by time, day by
  day), with one tool; she records her 这阵子 note with it, and that note is the only row written
  (her persona chain is not touched). A later tick finds this week's note written. The next
  Monday's review reads only that week's evidence, not the note she wrote the week before.
* ``model_call_fails`` — the model call fails; the tick does not raise it and nothing is written.
  The next tick writes the note.
* ``prompt_config_error`` — a release of the review prompt declares a variable no plugin gives.
  The first tick in the window fails on it before the model and writes nothing; the tick does not
  raise it. Once the prompt is fixed, the next tick writes the note.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.capabilities.errors import CapabilityTimeout
from app.data.persist import insert_idempotent
from app.infra.cst_time import CST
from app.living.household.received import ReceivedMessage
from app.living.services import WORLD
from tests.replay import seeds
from tests.replay.harness import Fail, Reply, ToolUse
from tests.replay.harness.prompts import config_errors

pytestmark = pytest.mark.integration

REVIEW = "living_persona_review"


def _at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=CST)


def _tick(replay):
    return lambda: replay.tick("persona-review")


async def _her_household_is_up(replay) -> None:
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    await replay.start("agent-service")


async def _said_to_her(replay, said: list[tuple[datetime, str, str]]) -> None:
    """What was said to her: ``(when, who, words)``. ``who`` is ``"phone"`` for bezhai in their
    private chat; anyone else came to her inbox (a sister in person, or world)."""
    for at, who, words in said:
        name = f"{at.isoformat()} {who}"
        if who == "phone":
            await seeds.bezhai_says(words, at=at, name=name)
            continue
        await insert_idempotent(
            ReceivedMessage(
                lane=replay.lane,
                persona_id="akao",
                message_id=str(seeds.fixed_id(f"received:{name}")),
                sender=who,
                body=words,
                message_time=at,
                wakes_recipient=True,
            )
        )


# The week 07-20 (Mon) .. 07-26 (Sun), with one message from the week before and the Sunday one
# stored first, to show the window and the order she reads them in.
LAST_WEEK = [
    (_at(7, 26, 20), "千凪", "相机我先借走啦，下周还你。"),
    (_at(7, 19, 21), "千凪", "明天开始我去图书馆打工，晚饭不用等我。"),
    (_at(7, 21, 18, 30), WORLD, "你看到绫奈抱着一摞新买的胶卷进门，放在客厅茶几上。"),
    (_at(7, 23, 22, 5), "phone", "论坛上有人回你那篇角色分析了，说写得很好"),
    (_at(7, 23, 22, 10), "绫奈", "姐姐教我冲胶卷好不好"),
]

WEEK_AFTER = [
    (_at(7, 28, 19), "千凪", "相机还你，胶卷我顺手帮你冲好了。"),
    (_at(8, 1, 15), "phone", "周六有空吗，一起去抹茶店"),
]

FIRST_NOTE = "这阵子绫奈想跟我学冲胶卷，我写的角色分析在论坛上有人认真回了。"
SECOND_NOTE = "这阵子相机回到我手上了，bezhai 约我周末去抹茶店。"


def _writes_down(note: str) -> Reply:
    """She records her 这阵子 note with the one tool she is given."""
    return Reply(tools=(ToolUse("write_down_these_days", {"these_days": note}),))


async def test_two_weeks(replay):
    await _her_household_is_up(replay)
    await _said_to_her(replay, LAST_WEEK)

    await replay.step(
        "a tick before the window does nothing", _tick(replay), at=_at(7, 27, 5, 55)
    )

    replay.model.script(REVIEW, _writes_down(FIRST_NOTE))
    await replay.step(
        "the first tick in the window: she writes down these days",
        _tick(replay),
        at=_at(7, 27, 6),
    )

    await replay.step(
        "a later tick finds this week's note written", _tick(replay), at=_at(7, 27, 6, 5)
    )

    await _said_to_her(replay, WEEK_AFTER)
    replay.model.script(REVIEW, _writes_down(SECOND_NOTE))
    await replay.step(
        "the next Monday: she writes a new note from that week alone",
        _tick(replay),
        at=_at(8, 3, 6),
    )

    replay.check("persona_review/two_weeks")


async def test_model_call_fails(replay):
    await _her_household_is_up(replay)
    await _said_to_her(replay, LAST_WEEK)

    replay.model.script(
        REVIEW,
        Fail(lambda: CapabilityTimeout("offline-model gave no answer within 180s")),
    )
    await replay.step(
        "the model call fails; the tick swallows it and nothing is written",
        _tick(replay),
        at=_at(7, 27, 6),
    )

    replay.model.script(REVIEW, _writes_down(FIRST_NOTE))
    await replay.step(
        "the next tick writes the note",
        _tick(replay),
        at=_at(7, 27, 6, 5),
    )

    replay.check("persona_review/model_call_fails")


async def test_prompt_config_error(replay):
    await _her_household_is_up(replay)
    await _said_to_her(replay, LAST_WEEK)
    errors_before = config_errors(REVIEW, "production", "akao")

    replay.misconfigure_prompt(REVIEW, "mood")
    await replay.step(
        "the review prompt fails its check; the tick swallows it and nothing is written",
        _tick(replay),
        at=_at(7, 27, 6),
    )
    failed = replay.steps[-1]
    assert failed["model_calls"] == []
    assert failed["rows"] == {}, "nothing is written before the model"
    assert config_errors(REVIEW, "production", "akao") == errors_before + 1

    replay.fix_prompt(REVIEW)
    replay.model.script(REVIEW, _writes_down(FIRST_NOTE))
    await replay.step(
        "the prompt is fixed; the next tick writes the note",
        _tick(replay),
        at=_at(7, 27, 6, 5),
    )

    replay.check("persona_review/prompt_config_error")
