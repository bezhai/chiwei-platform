"""Replay: her reading round, driven the way production drives it. A moment of hers picks a file
up (``read_a_bit`` emits a durable ``FilePickedUp``), and the scenario hands that message to the
durable queue's consumer (``read_a_round``), which fetches the file's bytes (tool-service signs
``files/<file_key>``, the store answers the GET), decodes and pages them, and runs the reading
agent.

Spec decision 7 moves this path from the durable edge to messaging (T3). What has to survive the
move is pinned here: the round's identity (``round_id``, derived from the moment and the file),
the reading-side dedupe (the same round is never read twice), and, for an error that reaches the
consumer, a single run whose message is rejected without a retry. A failed model call does not
reach it: the reading round is fail-soft and the message is acknowledged.

* ``continuation`` — her moment picks the file up, then fails; the same cell runs again and picks
  it up again under the same identity, so two copies of one round are queued. The first reads
  page 0 and writes her impression; the second is skipped without a model call. Her next moment
  sees how far she got, picks it up again (a new round), and that round reads on from page 1 to
  the end of the book.
* ``model_call_fails`` — the reading agent's model call fails: it is called once, the round
  writes nothing (impression and page untouched), and the message is acknowledged, not
  dead-lettered; nothing is queued for a retry.
* ``commit_fails`` — the round reads, but writing her impression fails at commit: the consumer
  raises and the message is rejected (dead-lettered by the broker); nothing is queued for a retry.
* ``killed_after_commit`` — the process dies right after her impression commits, before the
  delivery is marked done. RabbitMQ hands the unsettled message to the restarted process, which
  comes up after the dead process's five-minute claim has run out: the new process takes the claim
  over, and the reading side sees this round already committed and does not read it again (the
  reading-side dedupe on ``round_id``, the one that has to survive the move to messaging).
* ``killed_mid_round`` — the process dies while fetching the file. The message comes back to the
  restarted process a minute later, while the dead process's claim on it is still live, and it is
  settled without being read: that reading round is gone.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.capabilities._errors import CapabilityTimeout
from app.domain.reading_source import decode_pages
from app.infra.cst_time import CST, now_cst
from app.infra.rabbitmq import lane_queue
from app.living.moment import run_moment
from tests.replay import seeds
from tests.replay.harness import Fail, ProcessKilled, Reply, ToolUse

pytestmark = pytest.mark.integration

MOMENT = "living_life_moment"
READING = "book_reading_impression"

FILE_KEY = "file_v3_xiaowangzi"
FILE_NAME = "小王子.txt"

# Two pages: a first paragraph long enough that the second one does not fit on its page
# (``app.domain.reading_source.paginate``, 1800 characters a page, aligned to paragraphs).
_FIRST_PARAGRAPH = "我六岁那年，在一本描写原始森林的书里看到一幅精彩的插画。" * 64
_SECOND_PARAGRAPH = "小王子说：「重要的东西，用眼睛是看不见的。」"
BOOK = f"{_FIRST_PARAGRAPH}\n{_SECOND_PARAGRAPH}\n".encode()


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 25, hour, minute, tzinfo=CST)


def _queue(replay) -> str:
    return lane_queue("durable_file_picked_up_read_a_round", replay.lane)


async def _bezhai_sent_her_a_book(replay) -> None:
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    replay.broker.declare_inbox("world")
    await seeds.bezhai_sends(
        [{"kind": "file", "key": FILE_KEY, "meta": {"file_name": FILE_NAME}}],
        summary="[file]",
        at=_at(13, 30),
        name="book",
    )
    assert len(decode_pages(FILE_NAME, BOOK)) == 2, (
        "the book fixture should be two pages"
    )
    replay.objects.put(f"files/{FILE_KEY}", BOOK, "text/plain")
    await replay.start("agent-service")


def _moment(replay):
    return lambda: run_moment(lane=replay.lane, persona_id="akao", clock=now_cst)


def _picks_it_up() -> Reply:
    return Reply(tools=(ToolUse("read_a_bit", {"which": "小王子"}),))


def _done() -> Reply:
    return Reply(tools=(ToolUse("stop_for_now", {}),))


async def _she_picks_it_up(replay) -> None:
    replay.model.script(MOMENT, _picks_it_up(), _done())
    await replay.step("her moment picks the book up", _moment(replay), at=_at(14, 0))


async def _nothing_queued(replay) -> None:
    async def queued() -> list:
        return replay.broker.queued(_queue(replay))

    await replay.step("nothing is queued for a retry", queued)


async def test_continuation(replay):
    await _bezhai_sent_her_a_book(replay)

    replay.model.script(
        MOMENT,
        Reply(tools=(ToolUse("look_for_something_to_read", {}),)),
        _picks_it_up(),
        Fail(lambda: CapabilityTimeout("life-model gave no answer within 180s")),
    )
    await replay.step(
        "her moment picks the book up, then fails",
        _moment(replay),
        at=_at(14, 0),
        raises=CapabilityTimeout,
    )

    # The clock ticks again inside the same ten-minute cell: the same moment, run again.
    replay.model.script(MOMENT, _picks_it_up(), _done())
    await replay.step(
        "the same cell runs again and picks it up again", _moment(replay), at=_at(14, 1)
    )

    replay.model.script(
        READING,
        Reply(tools=(ToolUse("read", {"page_num": 0}),)),
        Reply(
            text="开头那幅蟒蛇吞大象的画，大人们都说是帽子。我有点替那个六岁的孩子难过。"
        ),
    )
    await replay.step(
        "the first copy is read",
        lambda: replay.broker.deliver(_queue(replay)),
        at=_at(14, 2),
    )
    await replay.step(
        "the second copy of the same round is not read again",
        lambda: replay.broker.deliver(_queue(replay)),
    )

    replay.model.script(
        MOMENT,
        Reply(tools=(ToolUse("look_for_something_to_read", {}),)),
        _picks_it_up(),
        _done(),
    )
    await replay.step(
        "her next moment picks it up again", _moment(replay), at=_at(14, 10)
    )

    replay.model.script(
        READING,
        Reply(tools=(ToolUse("read", {"page_num": 1}),)),
        Reply(tools=(ToolUse("read", {"page_num": 2}),)),
        Reply(
            text="读完了。那个孩子长大了还记得那幅画，最后那句话我想了很久：重要的东西，"
            "用眼睛是看不见的。"
        ),
    )
    await replay.step(
        "the new round reads on to the end",
        lambda: replay.broker.deliver(_queue(replay)),
        at=_at(14, 12),
    )

    replay.check("reading/continuation")


async def test_model_call_fails(replay):
    await _bezhai_sent_her_a_book(replay)
    await _she_picks_it_up(replay)

    replay.model.script(
        READING,
        Fail(lambda: CapabilityTimeout("book-reading gave no answer within 180s")),
    )
    await replay.step(
        "the reading agent's model call fails",
        lambda: replay.broker.deliver(_queue(replay)),
        at=_at(14, 2),
    )
    await _nothing_queued(replay)

    replay.check("reading/model_call_fails")


def _reads_the_first_page() -> tuple[Reply, Reply]:
    return (
        Reply(tools=(ToolUse("read", {"page_num": 0}),)),
        Reply(text="开头那幅蟒蛇吞大象的画，大人们都说是帽子。"),
    )


async def test_commit_fails(replay):
    await _bezhai_sent_her_a_book(replay)
    await _she_picks_it_up(replay)
    replay.fail_commits(lambda writes: "INSERT data_file_read" in writes)

    replay.model.script(READING, *_reads_the_first_page())
    await replay.step(
        "writing her impression fails at commit",
        lambda: replay.broker.deliver(_queue(replay)),
        at=_at(14, 2),
    )
    await _nothing_queued(replay)

    replay.check("reading/commit_fails")


async def test_killed_after_commit(replay):
    await _bezhai_sent_her_a_book(replay)
    await _she_picks_it_up(replay)
    replay.kill_after(
        lambda effect: (
            effect.get("db") == "commit" and "INSERT data_file_read" in effect["writes"]
        )
    )

    replay.model.script(READING, *_reads_the_first_page())
    await replay.step(
        "the process dies right after her impression commits",
        lambda: replay.broker.deliver(_queue(replay)),
        at=_at(14, 2),
        raises=ProcessKilled,
    )
    await replay.restart()
    assert replay.broker.requeue_unsettled() == 1

    await replay.step(
        "the unsettled message comes back after the dead process's claim ran out",
        lambda: replay.broker.deliver(_queue(replay)),
        at=_at(14, 8),
    )

    replay.check("reading/killed_after_commit")


async def test_killed_mid_round(replay):
    await _bezhai_sent_her_a_book(replay)
    await _she_picks_it_up(replay)
    replay.kill_after(lambda effect: effect.get("http", "").endswith("/get-url"))

    await replay.step(
        "the process dies while fetching the file",
        lambda: replay.broker.deliver(_queue(replay)),
        at=_at(14, 2),
        raises=ProcessKilled,
    )
    await replay.restart()
    assert replay.broker.requeue_unsettled() == 1

    await replay.step(
        "the unsettled message comes back to the new process",
        lambda: replay.broker.deliver(_queue(replay)),
        at=_at(14, 3),
    )

    replay.check("reading/killed_mid_round")
