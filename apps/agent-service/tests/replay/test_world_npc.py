"""Replay: an NPC's appearance, driven the way production drives it — the world main agent calls
``let_npc_appear`` inside a round, a one-off NPC agent (``app.world.npc.play_npc``) plays him,
and what he said and did goes to the perception judgement as the change.

* ``appears`` — the NPC agent looks something up and then acts; the perception judgement tells
  赤尾 right away; the main agent sees his words and who was told, and writes the record the
  encounter left behind.
* ``stays_silent`` — the NPC agent says and does nothing (every one of the agent loop's three
  attempts at the turn comes back empty): no judgement runs, nobody is told.
* ``model_call_fails`` — the NPC agent's model call fails with a 5xx. The NPC is not retried
  (one attempt), nothing is noted or sent, and the main agent is told he did not appear.

The ``world_npc`` fixture is the text of Langfuse prompt ``world_npc`` version 2, label
``coe-world`` (the prompt has no ``production`` label).
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from openai import InternalServerError

from app.infra.cst_time import CST
from tests.replay.harness import Fail, Reply, ToolUse

pytestmark = pytest.mark.integration

ROUND = "world_round"
NPC = "world_npc"
PERCEPTION = "world_perception"

AKAOS_DIGEST = (
    "我做了这些（按先后）：\n"
    "- 14:00 CST 改做 买抹茶，在 抹茶店\n"
    "- 14:00 CST 在柜台前点了一杯宇治抹茶\n"
    "做完这些，我在 抹茶店，正在 等店员做抹茶。"
)

CLERK = "小林，楼下抹茶店的店员"
SITUATION = (
    "14:01，抹茶店里。赤尾 刚在柜台点了一杯宇治抹茶，小林 正在柜台后面看今天的存货。"
)
CLERK_ACTS = (
    "14:01，抹茶店柜台后，小林 翻了翻存货单，抬头对赤尾说："
    "「不好意思，今天的宇治抹茶粉刚好用完了，换成焙茶拿铁可以吗？」"
)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 25, hour, minute, tzinfo=CST)


def _server_error() -> InternalServerError:
    request = httpx.Request("POST", "https://model-gateway.replay/v1/chat/completions")
    return InternalServerError(
        "Error code: 500 - upstream model unavailable",
        response=httpx.Response(500, request=request),
        body=None,
    )


async def _world_is_up(replay) -> None:
    # The sisters' inboxes are opened by agent-service, another process.
    for name in ("赤尾", "绫奈", "千凪"):
        replay.broker.declare_inbox(name)
    await replay.start("world")


def _akaos_digest_arrives(replay) -> str:
    return replay.message_arrives(
        sender="赤尾",
        recipient="world",
        body=AKAOS_DIGEST,
        message_id="akao-digest-1",
        time=_at(14, 0),
    )


_THE_CLERK_APPEARS = Reply(
    tools=(ToolUse("let_npc_appear", {"npc": CLERK, "situation": SITUATION}),)
)


def _wake_at_four() -> Reply:
    return Reply(
        tools=(
            ToolUse(
                "wake_me_at", {"at": "2026-07-25T16:00", "reason": "看看抹茶店后来怎样"}
            ),
        ),
    )


async def test_appears(replay):
    await _world_is_up(replay)
    inbox = _akaos_digest_arrives(replay)

    replay.model.script(
        ROUND,
        _THE_CLERK_APPEARS,
        Reply(
            tools=(
                ToolUse(
                    "write_record",
                    {
                        "path": "地方/抹茶店.md",
                        "text": "楼下的抹茶店，店员小林。7 月 25 日下午宇治抹茶粉用完了。",
                    },
                ),
            ),
        ),
        _wake_at_four(),
        Reply(text="这一轮看完了。"),
    )
    replay.model.script(
        NPC,
        Reply(tools=(ToolUse("list_records", {}),)),
        Reply(text=CLERK_ACTS),
    )
    replay.model.script(
        PERCEPTION,
        Reply(
            tools=(
                ToolUse(
                    "someone_notices",
                    {
                        "who": "赤尾",
                        "what": (
                            "14:01，抹茶店柜台前，店员小林翻了翻存货单，抬头对你说："
                            "「不好意思，今天的宇治抹茶粉刚好用完了，换成焙茶拿铁可以吗？」"
                        ),
                        "right_away": True,
                    },
                ),
            ),
        ),
        Reply(text="判断完了。"),
    )
    await replay.step(
        "the round lets the clerk appear",
        lambda: replay.broker.deliver(inbox),
        at=_at(14, 1),
    )

    replay.check("world_npc/appears")


async def test_stays_silent(replay):
    await _world_is_up(replay)
    inbox = _akaos_digest_arrives(replay)

    replay.model.script(
        ROUND,
        _THE_CLERK_APPEARS,
        _wake_at_four(),
        Reply(text="这一轮看完了。"),
    )
    # The agent loop asks again after an empty turn (up to three attempts in all), so he stays
    # silent only when all three come back empty.
    replay.model.script(NPC, Reply(text=""), Reply(text=""), Reply(text=""))
    await replay.step(
        "the clerk says and does nothing",
        lambda: replay.broker.deliver(inbox),
        at=_at(14, 1),
    )

    replay.check("world_npc/stays_silent")


async def test_model_call_fails(replay):
    await _world_is_up(replay)
    inbox = _akaos_digest_arrives(replay)

    replay.model.script(
        ROUND,
        _THE_CLERK_APPEARS,
        _wake_at_four(),
        Reply(text="这一轮看完了。"),
    )
    replay.model.script(NPC, Fail(_server_error))
    await replay.step(
        "the NPC agent's model call fails",
        lambda: replay.broker.deliver(inbox),
        at=_at(14, 1),
    )

    replay.check("world_npc/model_call_fails")
