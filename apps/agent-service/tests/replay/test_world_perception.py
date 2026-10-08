"""Replay: the perception judgement, driven the way production drives it — the world main agent
reports a change inside a round (``report_change``), which runs one judgement
(``app.world.perception.judge_who_notices``) and sends what it judged.

* ``right_away_and_later`` — one change, two people notice: one is called over right away
  (``wakes_recipient: true``), the other reads it when she next looks (``wakes_recipient:
  false``). Both notices are noted in ``unfinished.json`` before they are sent.
* ``retries_temporary_failure`` — the judgement's first attempt notes 千凪, then its model call
  fails with a 5xx. The whole judgement starts over from the same input; the second attempt
  notes 赤尾 only, and only that notice is sent: what the failed attempt judged is dropped.
* ``gives_up_after_retry_limit`` — every attempt fails (5xx, timeout, 5xx). After the third the
  main agent is told the change was not reported, nothing is noted or sent, and the round goes on.

The real retry delays are 2 s and 4 s (``RETRY_BASE_SECONDS`` doubling, capped at
``RETRY_MAX_SECONDS``). These scenarios shorten them to zero by patching the two constants in
``app.world.perception``. Only the event loop sleeps there; the frozen clock never moves, so
the baselines are the same as with the real delays.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from openai import APITimeoutError, InternalServerError

import app.world.perception as perception
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

CHANGE = "14:01，快递员在家门口按了门铃，送来一个寄给赤尾的包裹。"

_MODEL_URL = "https://model-gateway.replay/v1/chat/completions"


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 25, hour, minute, tzinfo=CST)


def _server_error() -> InternalServerError:
    request = httpx.Request("POST", _MODEL_URL)
    return InternalServerError(
        "Error code: 500 - upstream model unavailable",
        response=httpx.Response(500, request=request),
        body=None,
    )


def _timeout() -> APITimeoutError:
    return APITimeoutError(httpx.Request("POST", _MODEL_URL))


def _no_retry_delay(monkeypatch) -> None:
    monkeypatch.setattr(perception, "RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr(perception, "RETRY_MAX_SECONDS", 0.0)


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


def _round_reports_the_change(replay) -> None:
    replay.model.script(
        ROUND,
        Reply(tools=(ToolUse("report_change", {"change": CHANGE}),)),
        Reply(
            tools=(
                ToolUse(
                    "wake_me_at",
                    {"at": "2026-07-25T16:00", "reason": "看看包裹拆开了没有"},
                ),
            ),
        ),
        Reply(text="这一轮看完了。"),
    )


async def test_right_away_and_later(replay):
    await _world_is_up(replay)
    inbox = _akaos_digest_arrives(replay)

    _round_reports_the_change(replay)
    replay.model.script(
        PERCEPTION,
        Reply(
            tools=(
                ToolUse(
                    "someone_notices",
                    {
                        "who": "赤尾",
                        "what": "14:01，你在客厅听见门铃响了，门外的快递员说有你的包裹。",
                        "right_away": True,
                    },
                ),
                ToolUse(
                    "someone_notices",
                    {
                        "who": "绫奈",
                        "what": "14:01 前后，你在自己房间里隐约听见楼下门铃响了一声。",
                        "right_away": False,
                    },
                ),
            ),
        ),
        Reply(text="判断完了。"),
    )
    await replay.step(
        "the round reports a change two people notice",
        lambda: replay.broker.deliver(inbox),
        at=_at(14, 1),
    )

    replay.check("world_perception/right_away_and_later")


async def test_retries_temporary_failure(replay, monkeypatch):
    _no_retry_delay(monkeypatch)
    await _world_is_up(replay)
    inbox = _akaos_digest_arrives(replay)

    _round_reports_the_change(replay)
    replay.model.script(
        PERCEPTION,
        # The first attempt: notes 千凪, then its next model call fails.
        Reply(
            tools=(
                ToolUse(
                    "someone_notices",
                    {
                        "who": "千凪",
                        "what": "14:01，你在书房听见门铃响了一声。",
                        "right_away": False,
                    },
                ),
            ),
        ),
        Fail(_server_error),
        # The second attempt starts over and notes 赤尾 only.
        Reply(
            tools=(
                ToolUse(
                    "someone_notices",
                    {
                        "who": "赤尾",
                        "what": "14:01，你在客厅听见门铃响了，门外的快递员说有你的包裹。",
                        "right_away": True,
                    },
                ),
            ),
        ),
        Reply(text="判断完了。"),
    )
    await replay.step(
        "the judgement's model call fails once and the judgement starts over",
        lambda: replay.broker.deliver(inbox),
        at=_at(14, 1),
    )

    replay.check("world_perception/retries_temporary_failure")


async def test_gives_up_after_retry_limit(replay, monkeypatch):
    _no_retry_delay(monkeypatch)
    await _world_is_up(replay)
    inbox = _akaos_digest_arrives(replay)

    _round_reports_the_change(replay)
    replay.model.script(
        PERCEPTION,
        Reply(
            tools=(
                ToolUse(
                    "someone_notices",
                    {
                        "who": "赤尾",
                        "what": "14:01，你在客厅听见门铃响了，门外的快递员说有你的包裹。",
                        "right_away": True,
                    },
                ),
            ),
        ),
        Fail(_server_error),
        Fail(_timeout),
        Fail(_server_error),
    )
    await replay.step(
        "every attempt of the judgement fails",
        lambda: replay.broker.deliver(inbox),
        at=_at(14, 1),
    )

    replay.check("world_perception/gives_up_after_retry_limit")
