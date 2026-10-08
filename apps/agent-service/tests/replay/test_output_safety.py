"""Replay: the output check on what she sends, inside her regular round (``run_moment``), which
is where production runs it (``send_message`` checks before it claims and hands off).

Each scenario is one round in which bezhai has written to her and she sends to his private chat.

* ``banned_word`` — the banned-word set is read under the bare key ``banned_words``: the Redis
  capability adds no lane prefix (a coe lane has a Redis of its own). A word that is only under
  ``<lane>:banned_words`` does not stop her message; the guard model judges it safe and it goes
  out. A word under ``banned_words`` (matched after spaces are removed and case is folded) stops
  the next one before the guard model is asked: nothing is claimed, nothing is sent, she is told.
* ``unsafe_at_threshold`` — the guard model says unsafe with confidence 0.7: stopped. Then unsafe
  with confidence 0.69: sent.
* ``check_fails`` — the guard model call fails: the message is sent anyway (fail-open).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.capabilities._errors import CapabilityCallFailed
from app.infra.cst_time import CST, now_cst
from app.living.moment import run_moment
from tests.replay import seeds
from tests.replay.harness import Fail, Reply, ToolUse

pytestmark = pytest.mark.integration

MOMENT = "living_life_moment"
GUARD = "guard_output_safety"


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 25, hour, minute, tzinfo=CST)


async def _bezhai_has_written(replay) -> None:
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    replay.broker.declare_inbox(
        "world", answers=lambda question: "客厅里只有你一个人，窗外开始下小雨。"
    )
    await replay.start("agent-service")
    await seeds.bezhai_says("在吗？说说你今天干嘛了", at=_at(13, 55), name="dm-1")


def _moment(replay):
    return lambda: run_moment(lane=replay.lane, persona_id="akao", clock=now_cst)


def _sends(what: str) -> Reply:
    return Reply(
        tools=(
            ToolUse(
                "send_message",
                {"what": what, "channel_id": str(seeds.DM_WITH_BEZHAI)},
            ),
        )
    )


def _stops() -> Reply:
    return Reply(tools=(ToolUse("stop_for_now", {}),))


def _safe() -> Reply:
    return Reply(data={"is_unsafe": False, "confidence": 0.03})


BANNED_KEYS = ("banned_words", "coe-replay:banned_words")


async def test_banned_word(replay):
    await _bezhai_has_written(replay)
    assert BANNED_KEYS[1] == f"{replay.lane}:banned_words"
    await replay.redis.sadd(BANNED_KEYS[0], "darkroom")
    await replay.redis.sadd(BANNED_KEYS[1], "胶片")
    try:
        replay.model.script(
            MOMENT,
            _sends("在家整理胶片呢。"),
            _sends("下午一直待在 Dark Room 里。"),
            _stops(),
        )
        replay.model.script(GUARD, _safe())
        await replay.step(
            "a word only under the lane-prefixed key does not stop her message; "
            "one under the bare key stops the next",
            _moment(replay),
            at=_at(14, 0),
        )
    finally:
        # Every replay's fakeredis gets the same server (its random host comes from the
        # replayed uuid4), so a word left behind would stop later scenarios' messages.
        await replay.redis.delete(*BANNED_KEYS)

    replay.check("output_safety/banned_word")


async def test_unsafe_at_threshold(replay):
    await _bezhai_has_written(replay)

    replay.model.script(
        MOMENT,
        _sends("今天去暗房冲了两卷胶片。"),
        _sends("今天在家冲胶片，药水味有点重。"),
        _stops(),
    )
    replay.model.script(
        GUARD,
        Reply(data={"is_unsafe": True, "confidence": 0.7}),
        Reply(data={"is_unsafe": True, "confidence": 0.69}),
    )
    await replay.step(
        "unsafe at 0.7 is stopped, unsafe at 0.69 is sent",
        _moment(replay),
        at=_at(14, 0),
    )

    replay.check("output_safety/unsafe_at_threshold")


async def test_check_fails(replay):
    await _bezhai_has_written(replay)

    replay.model.script(MOMENT, _sends("在家整理胶片呢。"), _stops())
    replay.model.script(
        GUARD, Fail(lambda: CapabilityCallFailed("guard-model returned HTTP 503"))
    )
    await replay.step(
        "the guard model call fails; the message is sent unchecked",
        _moment(replay),
        at=_at(14, 0),
    )

    replay.check("output_safety/check_fails")
