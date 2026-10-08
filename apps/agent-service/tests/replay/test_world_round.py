"""Replay: the world main agent's round, driven the way messaging drives it (a delivery to the
``world`` inbox, handed to the inbox's consumer).

* ``continuation`` — the process starts (and schedules its start-up wake); a resident's digest
  wakes the first round, which lists and writes a record, reports a change (a perception
  judgement inside the round tells a resident) and sets its next wake. The scheduled queue then
  hands over both wakes: the start-up wake has been replaced and runs no round; the wake the
  first round set runs the second round, which continues the stored history.
* ``model_call_fails`` — the round's second model call fails: the delivery fails, messaging
  schedules a retry; the redelivered message runs the round again and it completes.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.capabilities._errors import CapabilityTimeout
from app.infra.cst_time import CST
from tests.replay.harness import Fail, Reply, ToolUse

pytestmark = pytest.mark.integration

ROUND = "world_round"
PERCEPTION = "world_perception"

AKAOS_DIGEST = (
    "我做了这些（按先后）：\n"
    "- 14:00 CST 改做 整理胶片，在 家/客厅\n"
    "- 14:00 CST 把胶片一卷卷摆到茶几上\n"
    "做完这些，我在 家/客厅，正在 整理胶片。"
)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 25, hour, minute, tzinfo=CST)


async def _world_is_up(replay) -> None:
    # The sisters' inboxes are opened by agent-service, another process.
    for name in ("赤尾", "绫奈", "千凪"):
        replay.broker.declare_inbox(name)
    await replay.start("world")


def _akaos_digest_arrives(replay, message_id: str = "akao-digest-1") -> str:
    return replay.message_arrives(
        sender="赤尾",
        recipient="world",
        body=AKAOS_DIGEST,
        message_id=message_id,
        time=_at(14, 0),
    )


async def test_continuation(replay):
    await _world_is_up(replay)
    inbox = _akaos_digest_arrives(replay)

    replay.model.script(
        ROUND,
        Reply(tools=(ToolUse("list_records", {}), ToolUse("list_senders", {}))),
        Reply(
            tools=(
                ToolUse(
                    "write_record",
                    {"path": "地方/家.md", "text": "客厅的茶几上摊着赤尾的胶片。"},
                ),
            ),
        ),
        Reply(
            tools=(ToolUse("report_change", {"change": "14:01，家附近下起了小雨。"}),)
        ),
        Reply(
            tools=(
                ToolUse(
                    "wake_me_at", {"at": "2026-07-25T16:00", "reason": "看看雨停了没有"}
                ),
            ),
        ),
        Reply(text="这一轮看完了。"),
    )
    replay.model.script(
        PERCEPTION,
        Reply(
            tools=(
                ToolUse(
                    "someone_notices",
                    {
                        "who": "赤尾",
                        "what": "你听见客厅窗外下起了小雨。",
                        "right_away": False,
                    },
                ),
            ),
        ),
        Reply(text="判断完了。"),
    )
    await replay.step(
        "赤尾's digest wakes the first round",
        lambda: replay.broker.deliver(inbox),
        at=_at(14, 1),
    )

    async def hand_over_due_wakes() -> list[str]:
        return [
            await replay.broker.deliver(replay.scheduled()),
            await replay.broker.deliver(replay.scheduled()),
        ]

    await replay.step(
        "the scheduled queue hands over the due wakes",
        hand_over_due_wakes,
        at=_at(16, 0),
    )
    await replay.step(
        "the replaced start-up wake arrives", lambda: replay.broker.deliver(inbox)
    )

    replay.model.script(
        ROUND,
        Reply(tools=(ToolUse("read_record", {"path": "地方/家.md"}),)),
        Reply(
            tools=(
                ToolUse(
                    "wake_me_at", {"at": "2026-07-25T20:00", "reason": "晚上再看看家里"}
                ),
            ),
        ),
        Reply(text="雨停了，记录不用改。"),
    )
    await replay.step(
        "its own 16:00 wake runs the second round", lambda: replay.broker.deliver(inbox)
    )

    replay.check("world_round/continuation")


async def test_model_call_fails(replay):
    await _world_is_up(replay)
    inbox = _akaos_digest_arrives(replay)

    replay.model.script(
        ROUND,
        Reply(tools=(ToolUse("list_records", {}),)),
        Fail(lambda: CapabilityTimeout("offline-model gave no answer within 180s")),
    )
    await replay.step(
        "the round's second model call fails",
        lambda: replay.broker.deliver(inbox),
        at=_at(14, 1),
    )

    replay.model.script(
        ROUND,
        Reply(tools=(ToolUse("list_records", {}),)),
        Reply(
            tools=(
                ToolUse(
                    "wake_me_at",
                    {"at": "2026-07-25T16:00", "reason": "过两个小时再看看"},
                ),
            ),
        ),
        Reply(text="这一轮看完了。"),
    )
    await replay.step(
        "the redelivered message runs the round again",
        lambda: replay.broker.deliver(inbox),
        at=_at(14, 2),
    )

    replay.check("world_round/model_call_fails")
