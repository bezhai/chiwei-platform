"""Replay: her regular round (the living moment), driven the way the life clock drives it
(``run_moment`` with the clock read when the round gets its turn).

* ``continuation`` — two rounds ten minutes apart. The first starts cold (no history) and uses
  most of what a round touches: a thought with a provider signature, switching and keeping in
  mind, speech to a sister and an act (the round-end digest to world and the direct message to
  the sister), asking world what the room is like, reading the phone, sending a message (the
  output safety check and the ``chat_response`` queue), and stopping with a call after the
  terminating one (that call never runs; its result is filled in for the stored history). The
  second continues the stored history (signature and filled-in result included), takes the sent
  message back (the ``recall`` queue) and ends on plain text.
* ``model_call_fails`` — the second model call of a round fails. The round fails; the clock
  ticks again inside the same grid cell and the round runs again under the same identity.
* ``commit_fails`` — the round's own transaction (the moment, phone and inbox reads) fails at
  commit. The round fails; the same cell runs again.
* ``send_fails`` — the broker does not confirm the round-end digest to world. The round still
  counts; the next round sends the digest again, under the same message id, before it starts.
* ``killed_before_history_is_written`` — the process dies right after the round's transaction
  commits, before her history is written. The next process's next round notices the gap and lays
  her state out again.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest
from sqlalchemy.exc import OperationalError

from app.capabilities._errors import CapabilityTimeout
from app.infra.cst_time import CST, now_cst
from app.living.moment import run_moment
from tests.replay import seeds
from tests.replay.harness import Fail, ProcessKilled, Reply, Request, ToolUse

pytestmark = pytest.mark.integration

MOMENT = "living_life_moment"
GUARD = "guard_output_safety"


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 25, hour, minute, tzinfo=CST)


async def _her_household_is_up(replay) -> None:
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    # world runs in its own process: its inbox is there, and it answers what she asks.
    replay.broker.declare_inbox(
        "world", answers=lambda question: "客厅里只有你一个人，窗外开始下小雨。"
    )
    await replay.start("agent-service")


def _moment(replay):
    return lambda: run_moment(lane=replay.lane, persona_id="akao", clock=now_cst)


def _take_back_what_she_sent(request: Request) -> Reply:
    """Copy the handle out of the send receipt in her history, as she would."""
    receipt = next(r for r in request.tool_results() if r.startswith("发出去了"))
    handle = re.search(r"［(.+?)］", receipt).group(1)
    return Reply(tools=(ToolUse("take_back_message", {"message_id": handle}),))


async def test_continuation(replay):
    await _her_household_is_up(replay)
    await seeds.bezhai_says("在吗？下午有空吗", at=_at(13, 55), name="dm-1")

    inbox = replay.message_arrives(
        sender="绫奈",
        recipient="赤尾",
        body="姐，晚上一起看那部片子吗？",
        message_id="ayana-1",
        time=_at(13, 58),
    )
    await replay.step(
        "绫奈's message reaches her inbox",
        lambda: replay.broker.deliver(inbox),
        at=_at(13, 58),
    )

    replay.model.script(
        MOMENT,
        Reply(
            thought="下午光线好，正适合整理胶片。",
            thought_signature=b"sig-moment-1",
            tools=(
                ToolUse(
                    "switch_to",
                    {
                        "doing": "整理胶片",
                        "place": "家/客厅",
                        "because": "下午光线正好",
                    },
                ),
                ToolUse(
                    "keep_in_mind",
                    {"still_on_my_mind": ["[2026-07-25 20:00] 和绫奈看片子"]},
                ),
            ),
        ),
        Reply(
            tools=(
                ToolUse("say", {"what": "好呀，八点？", "to": ["绫奈"]}),
                ToolUse("act", {"what": "把胶片一卷卷摆到茶几上"}),
                ToolUse("look_around", {}),
            ),
        ),
        Reply(
            tools=(ToolUse("look_at_phone", {"channel_id": str(seeds.DM_WITH_BEZHAI)}),)
        ),
        Reply(
            tools=(
                ToolUse(
                    "send_message",
                    {
                        "what": "在的，下午在家整理胶片。",
                        "channel_id": str(seeds.DM_WITH_BEZHAI),
                    },
                ),
            ),
        ),
        # A call after the terminating one never runs; its result is filled in for the
        # stored history.
        Reply(tools=(ToolUse("stop_for_now", {}), ToolUse("look_around", {}))),
    )
    replay.model.script(GUARD, Reply(data={"is_unsafe": False, "confidence": 0.05}))
    await replay.step("first moment (cold start)", _moment(replay), at=_at(14, 0))

    await seeds.bezhai_says("好的，那你忙", at=_at(14, 5), name="dm-2")
    replay.model.script(
        MOMENT,
        _take_back_what_she_sent,
        Reply(text="算了，那句不发了，接着整理。"),
    )
    await replay.step(
        "second moment (continues the history)", _moment(replay), at=_at(14, 10)
    )

    replay.check("living_moment/continuation")


async def test_model_call_fails(replay):
    await _her_household_is_up(replay)

    replay.model.script(
        MOMENT,
        Reply(
            tools=(
                ToolUse(
                    "switch_to",
                    {"doing": "泡抹茶", "place": "家/厨房", "because": "有点渴"},
                ),
            ),
        ),
        Fail(lambda: CapabilityTimeout("life-model gave no answer within 180s")),
    )
    await replay.step(
        "a moment whose second model call fails",
        _moment(replay),
        at=_at(14, 0),
        raises=CapabilityTimeout,
    )

    # The clock ticks again inside the same ten-minute cell: the same moment, run again.
    replay.model.script(
        MOMENT,
        Reply(
            tools=(
                ToolUse(
                    "switch_to",
                    {"doing": "泡抹茶", "place": "家/厨房", "because": "有点渴"},
                ),
            ),
        ),
        Reply(tools=(ToolUse("stop_for_now", {}),)),
    )
    await replay.step("the same cell runs again", _moment(replay), at=_at(14, 1))

    replay.check("living_moment/model_call_fails")


def _she_makes_tea() -> Reply:
    return Reply(
        tools=(
            ToolUse(
                "switch_to",
                {"doing": "泡抹茶", "place": "家/厨房", "because": "有点渴"},
            ),
            ToolUse("say", {"what": "要喝抹茶吗？", "to": ["绫奈"]}),
        ),
    )


async def test_commit_fails(replay):
    await _her_household_is_up(replay)
    replay.fail_commits(lambda writes: "INSERT data_life_moment" in writes)

    replay.model.script(
        MOMENT, _she_makes_tea(), Reply(tools=(ToolUse("stop_for_now", {}),))
    )
    await replay.step(
        "a moment whose own transaction fails at commit",
        _moment(replay),
        at=_at(14, 0),
        raises=OperationalError,
    )

    replay.model.script(
        MOMENT, _she_makes_tea(), Reply(tools=(ToolUse("stop_for_now", {}),))
    )
    await replay.step("the same cell runs again", _moment(replay), at=_at(14, 1))

    replay.check("living_moment/commit_fails")


async def test_send_fails(replay):
    await _her_household_is_up(replay)
    replay.broker.refuse_confirms(lambda rk: rk.startswith("inbox.world."))

    replay.model.script(
        MOMENT, _she_makes_tea(), Reply(tools=(ToolUse("stop_for_now", {}),))
    )
    await replay.step(
        "a moment whose digest to world is not confirmed",
        _moment(replay),
        at=_at(14, 0),
    )

    replay.model.script(MOMENT, Reply(text="继续"))
    await replay.step(
        "the next moment sends the digest again first", _moment(replay), at=_at(14, 10)
    )

    replay.check("living_moment/send_fails")


async def test_killed_before_history_is_written(replay):
    await _her_household_is_up(replay)
    replay.kill_after(
        lambda effect: (
            effect.get("db") == "commit"
            and "INSERT data_life_moment" in effect["writes"]
        )
    )

    replay.model.script(
        MOMENT, _she_makes_tea(), Reply(tools=(ToolUse("stop_for_now", {}),))
    )
    await replay.step(
        "the process dies right after the moment commits",
        _moment(replay),
        at=_at(14, 0),
        raises=ProcessKilled,
    )
    await replay.restart()

    replay.model.script(MOMENT, Reply(text="继续"))
    await replay.step(
        "the next moment finds her history one round short",
        _moment(replay),
        at=_at(14, 10),
    )

    replay.check("living_moment/killed_before_history_is_written")
