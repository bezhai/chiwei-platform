"""Replay: world answering a question, driven the way production drives it — a question another
process asked (``app.messaging.sending.ask``) lands on world's question queue and is delivered
to its consumer, which runs the answer agent (``app.world.answer.answer_question``).

Each scenario starts world, writes one record the answer agent can read (a seed, not part of
any step), gives the asker a reply queue the way its process would have, and puts 绫奈's
question on ``questions_world_<lane>`` with the two headers ``ask`` sends: ``x-reply-rk`` (where
the answer goes) and ``x-answer-by`` (when the asker stops waiting).

* ``answered`` — the answer agent reads the record and answers; the answer is recorded and
  published to the asker's reply queue.
* ``answer_by_passed`` — the question is delivered after its answer-by time. Recorded as the
  code handles it today: no model call and no reply to the asker; the delivery is acknowledged.
* ``model_call_fails`` — the answer agent's model call fails with a 5xx: no retry, and the asker
  gets "no answer" with the reason.

The ``world_answer`` fixture is the text of Langfuse prompt ``world_answer`` version 2, label
``coe-world`` (the prompt has no ``production`` label).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from openai import InternalServerError

from app.infra.cst_time import CST
from app.infra.rabbitmq import lane_queue
from app.messaging.broker import headers, question_route, reply_route
from app.messaging.message import Kind, new_message
from app.messaging.sending import ANSWER_BY_HEADER, REPLY_RK_HEADER
from app.world import records
from tests.replay.harness import Fail, Reply, ToolUse

pytestmark = pytest.mark.integration

ANSWER = "world_answer"

HOME = "地方/家.md"
HOME_RECORD = "家在一栋老公寓的三楼。客厅朝南，茶几上摊着赤尾的胶片。"

# The asker's private reply address, as ``ask`` puts it in ``x-reply-rk`` (without the lane).
ASKERS_REPLY_RK = "messaging.reply.ayana-asks"


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 25, hour, minute, tzinfo=CST)


def _server_error() -> InternalServerError:
    request = httpx.Request("POST", "https://model-gateway.replay/v1/chat/completions")
    return InternalServerError(
        "Error code: 500 - upstream model unavailable",
        response=httpx.Response(500, request=request),
        body=None,
    )


async def _world_is_up_with_a_record(replay) -> None:
    await replay.start("world")
    records.write(HOME, HOME_RECORD, expected=None)
    # The asker's process opened its private reply queue before asking.
    await replay.broker.declare_private_queue(
        None, reply_route(ASKERS_REPLY_RK), replay.lane
    )


def _ayana_asks(replay, *, asked: datetime, waits: timedelta) -> str:
    """绫奈's question lands on world's question queue; returns the queue name."""
    question = new_message(
        sender="绫奈",
        recipient="world",
        body="家里客厅现在什么样？",
        kind=Kind.QUESTION,
        time=asked,
        message_id="ayana-question-1",
    )
    answer_by = (asked + waits).astimezone(UTC)
    queue = lane_queue(question_route("world").queue, replay.lane)
    replay.broker.inject(
        queue,
        question.to_json(),
        headers(
            {REPLY_RK_HEADER: ASKERS_REPLY_RK, ANSWER_BY_HEADER: answer_by.isoformat()}
        ),
    )
    return queue


async def test_answered(replay):
    await _world_is_up_with_a_record(replay)
    questions = _ayana_asks(replay, asked=_at(15, 0), waits=timedelta(seconds=60))

    replay.model.script(
        ANSWER,
        Reply(tools=(ToolUse("read_record", {"path": HOME}),)),
        Reply(text="家里客厅朝南，下午这会儿有太阳照进来；茶几上摊着赤尾的胶片。"),
    )
    await replay.step(
        "绫奈's question is delivered and answered",
        lambda: replay.broker.deliver(questions),
        at=_at(15, 0),
    )

    replay.check("world_answer/answered")


async def test_answer_by_passed(replay):
    await _world_is_up_with_a_record(replay)
    questions = _ayana_asks(replay, asked=_at(14, 58), waits=timedelta(seconds=60))

    await replay.step(
        "绫奈's question is delivered after she stopped waiting",
        lambda: replay.broker.deliver(questions),
        at=_at(15, 0),
    )

    replay.check("world_answer/answer_by_passed")


async def test_model_call_fails(replay):
    await _world_is_up_with_a_record(replay)
    questions = _ayana_asks(replay, asked=_at(15, 0), waits=timedelta(seconds=60))

    replay.model.script(ANSWER, Fail(_server_error))
    await replay.step(
        "the answer agent's model call fails",
        lambda: replay.broker.deliver(questions),
        at=_at(15, 0),
    )

    replay.check("world_answer/model_call_fails")
