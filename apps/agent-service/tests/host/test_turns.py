"""Turns: one participant's queue of turns on one line (``app.host.turns``).

What these tests pin (agent-service plugin host, phase 2 design, section 3.2):

* how a source's signals coalesce: every signal queued (``QUEUE_EACH``); a signal dropped while
  the same source already has one queued or running (``DROP_WHILE_ACTIVE``); signals joining the
  next batch until that batch's ``due`` closes it (``JOIN_NEXT``);
* first come, first served on one queue, whatever the source; lines and participants do not block
  each other;
* Turns never reads the clock: it hands the clock to ``due``, which reads it when it wants to;
* ``due`` returning ``None`` runs nothing; ``hold_limit`` cuts the holder off with ``TimeoutError``
  and lets the queue move on;
* errors of ``due`` and ``run`` reach the leader as they are and the joiners as
  :class:`JoinedTurnFailed`; a leader cancelled before its batch is closed fails its joiners
  instead of leaving them waiting.

The sixteen scenarios of ``tests/living/test_serial.py`` (the in-process lock her rounds took
before Turns) are carried over one for one: the four of ``at_most_one`` are the
``DROP_WHILE_ACTIVE`` tests of the first section, the twelve of ``hold`` the last section.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from app.host import Batch, Coalesce, JoinedTurnFailed, Signal, Turns, WakeSource
from app.infra.cst_time import CST

T0 = datetime(2026, 7, 25, 14, 0, tzinfo=CST)


@dataclass(frozen=True)
class _Due:
    reason: str
    signals: tuple[Signal, ...] = ()
    now: datetime | None = None


def _stopped_at(at: datetime = T0) -> Callable[[], datetime]:
    return lambda: at


def _turns(
    participant: str = "akao",
    line: str = "round",
    *,
    hold_limit: timedelta | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Turns:
    return Turns(participant, line, hold_limit=hold_limit, clock=clock or _stopped_at())


def _source(name: str, coalesce: Coalesce, *, due=None, run=None) -> WakeSource:
    async def closes_and_runs(participant: str, batch: Batch, clock) -> _Due:
        return _Due(reason=f"{name} for {participant}", signals=tuple(batch.close()))

    async def hands_back(due: _Due) -> _Due:
        return due

    return WakeSource(name, coalesce, due or closes_and_runs, run or hands_back)


def _holding(name: str = "holder") -> tuple[WakeSource, asyncio.Event, asyncio.Event]:
    """A source whose turn holds the queue until ``release`` is set; ``inside`` is set once it
    holds it."""
    inside, release = asyncio.Event(), asyncio.Event()

    async def run(due: _Due) -> str:
        inside.set()
        await release.wait()
        return name

    return _source(name, Coalesce.QUEUE_EACH, run=run), inside, release


async def _settle() -> None:
    """Let every task that can move, move."""
    for _ in range(20):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# QUEUE_EACH, DROP_WHILE_ACTIVE, and one queue for every source
# ---------------------------------------------------------------------------


async def test_every_signal_of_a_queue_each_source_gets_a_turn_of_its_own_in_order():
    turns = _turns(line="day-page")
    holder, inside, release = _holding()
    ran: list[datetime] = []

    async def run(due: _Due) -> datetime:
        ran.append(due.signals[0].at)
        return due.signals[0].at

    day_page = _source("day-page", Coalesce.QUEUE_EACH, run=run)
    held = asyncio.create_task(turns.take(holder, Signal(at=T0)))
    await inside.wait()
    waiting = [
        asyncio.create_task(turns.take(day_page, Signal(at=T0 + timedelta(minutes=m))))
        for m in (5, 10, 15)
    ]
    await _settle()
    assert ran == [], "a queued signal ran while the queue was held"

    release.set()
    assert await held == "holder"
    assert await asyncio.gather(*waiting) == [
        T0 + timedelta(minutes=m) for m in (5, 10, 15)
    ]
    assert ran == [T0 + timedelta(minutes=m) for m in (5, 10, 15)]


async def test_a_signal_is_dropped_at_once_while_its_source_has_one_running():
    turns = _turns()
    inside, leave = asyncio.Event(), asyncio.Event()

    async def run(due: _Due) -> str:
        inside.set()
        await leave.wait()
        return "ran"

    regular = _source("regular", Coalesce.DROP_WHILE_ACTIVE, run=run)
    first = asyncio.create_task(turns.take(regular, Signal(at=T0)))
    await inside.wait()

    async with asyncio.timeout(1.0):
        assert await turns.take(regular, Signal(at=T0 + timedelta(minutes=1))) is None

    leave.set()
    assert await first == "ran"


async def test_a_signal_is_dropped_at_once_while_its_source_has_one_queued():
    """Her round hangs; the clock keeps ticking. Only one tick of a source waits behind it; the
    ones after it are dropped there and then. The one that waits decides on what is true when it
    gets its turn, which covers what the dropped ones would have decided."""
    turns = _turns()
    holder, inside, release = _holding()
    regular = _source("regular", Coalesce.DROP_WHILE_ACTIVE)

    held = asyncio.create_task(turns.take(holder, Signal(at=T0)))
    await inside.wait()
    ticks = [
        asyncio.create_task(turns.take(regular, Signal(at=T0 + timedelta(minutes=m))))
        for m in (1, 2, 3)
    ]
    await _settle()

    assert [t.done() for t in ticks] == [False, True, True]
    assert [t.result() for t in ticks[1:]] == [None, None]
    release.set()
    await held
    assert (await ticks[0]).signals == (Signal(at=T0 + timedelta(minutes=1)),)


async def test_a_source_that_dropped_signals_takes_one_again_once_its_turn_is_over():
    turns = _turns()
    regular = _source("regular", Coalesce.DROP_WHILE_ACTIVE)

    assert await turns.take(regular, Signal(at=T0)) is not None
    assert await turns.take(regular, Signal(at=T0 + timedelta(minutes=1))) is not None


async def test_a_source_is_let_go_when_its_turn_fails_or_is_cancelled():
    """The turn inside failed, was cut off at its cap, or was cancelled (a deploy): the source
    takes signals again. Otherwise that clock would never get a turn again."""
    turns = _turns()

    async def fails(due: _Due) -> None:
        raise RuntimeError("this round failed")

    with pytest.raises(RuntimeError, match="this round failed"):
        await turns.take(
            _source("regular", Coalesce.DROP_WHILE_ACTIVE, run=fails), Signal(at=T0)
        )
    assert await turns.take(
        _source("regular", Coalesce.DROP_WHILE_ACTIVE), Signal(at=T0)
    )

    started = asyncio.Event()

    async def waits_forever(due: _Due) -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        turns.take(
            _source("regular", Coalesce.DROP_WHILE_ACTIVE, run=waits_forever),
            Signal(at=T0),
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await turns.take(
        _source("regular", Coalesce.DROP_WHILE_ACTIVE), Signal(at=T0)
    )


async def test_two_sources_do_not_drop_each_others_signals_and_queue_first_come_first_served():
    """Her regular rhythm and her early wakes share one queue: neither drops the other's signal,
    and they get their turns in the order they came."""
    turns = _turns()
    holder, inside, release = _holding()
    order: list[str] = []

    def recording(name: str) -> WakeSource:
        async def run(due: _Due) -> str:
            order.append(name)
            return name

        return _source(name, Coalesce.DROP_WHILE_ACTIVE, run=run)

    held = asyncio.create_task(turns.take(holder, Signal(at=T0)))
    await inside.wait()
    nudge = asyncio.create_task(turns.take(recording("nudge"), Signal(at=T0)))
    await _settle()
    regular = asyncio.create_task(turns.take(recording("regular"), Signal(at=T0)))
    await _settle()

    assert not nudge.done() and not regular.done(), "one source turned the other away"
    release.set()
    await held
    assert (await nudge, await regular) == ("nudge", "regular")
    assert order == ["nudge", "regular"]


async def test_lines_do_not_block_each_other():
    """Her diary has a queue of its own: a round of hers that hangs does not hold it up."""
    rounds, pages = _turns(line="round"), _turns(line="day-page")
    holder, inside, release = _holding()

    held = asyncio.create_task(rounds.take(holder, Signal(at=T0)))
    await inside.wait()
    async with asyncio.timeout(1.0):
        assert await pages.take(_source("day-page", Coalesce.QUEUE_EACH), Signal(at=T0))
    release.set()
    await held


# ---------------------------------------------------------------------------
# due: when it runs, the clock, None
# ---------------------------------------------------------------------------


async def test_turns_never_reads_the_clock_itself():
    def no_reading(_=None) -> datetime:
        raise AssertionError(
            "Turns read the clock; only due reads it, when it wants to"
        )

    turns = _turns(clock=no_reading)

    async def due(participant: str, batch: Batch, clock) -> _Due:
        batch.close()
        return _Due(reason="never looked at the clock")

    assert await turns.take(
        _source("world-inbox", Coalesce.JOIN_NEXT, due=due), Signal(at=T0)
    )


async def test_due_reads_the_time_at_the_moment_it_reads_the_clock():
    """A due that blocks (a query) across the turn of a minute and reads the clock afterwards gets
    the time after the minute turned; one that reads it first gets the time before."""
    now = [T0 + timedelta(seconds=59)]
    turns = _turns(clock=lambda: now[0])
    queried = asyncio.Event()
    answered = asyncio.Event()

    async def reads_after_the_query(participant: str, batch: Batch, clock) -> _Due:
        batch.close()
        queried.set()
        await answered.wait()
        return _Due(reason="read after", now=clock())

    async def reads_first(participant: str, batch: Batch, clock) -> _Due:
        read = clock()
        queried.set()
        await answered.wait()
        return _Due(reason="read first", now=read)

    for due, expected in (
        (reads_after_the_query, T0 + timedelta(minutes=1)),
        (reads_first, T0 + timedelta(seconds=59)),
    ):
        now[0] = T0 + timedelta(seconds=59)
        queried.clear()
        answered.clear()
        task = asyncio.create_task(
            turns.take(_source("s", Coalesce.QUEUE_EACH, due=due), Signal(at=T0))
        )
        await queried.wait()
        now[0] = T0 + timedelta(minutes=1)
        answered.set()
        assert (await task).now == expected


async def test_due_is_asked_only_once_the_queue_is_its():
    """She is judged on what is true when she gets her turn, not when the signal came."""
    turns = _turns()
    holder, inside, release = _holding()
    asked: list[str] = []

    async def due(participant: str, batch: Batch, clock) -> _Due:
        asked.append(participant)
        return _Due(reason="asked")

    held = asyncio.create_task(turns.take(holder, Signal(at=T0)))
    await inside.wait()
    waiting = asyncio.create_task(
        turns.take(
            _source("regular", Coalesce.DROP_WHILE_ACTIVE, due=due), Signal(at=T0)
        )
    )
    await _settle()
    assert asked == []

    release.set()
    await held
    await waiting
    assert asked == ["akao"]


async def test_nothing_runs_when_due_says_no():
    turns = _turns()

    async def run(due: _Due) -> None:
        raise AssertionError("run was called after due said no")

    async def says_no(participant: str, batch: Batch, clock) -> None:
        return None

    assert (
        await turns.take(
            _source("regular", Coalesce.DROP_WHILE_ACTIVE, due=says_no, run=run),
            Signal(at=T0),
        )
        is None
    )


async def test_the_reason_of_a_turn_that_runs_is_logged_with_who_and_which_queue(
    caplog,
):
    turns = _turns("chinagi", "round")

    with caplog.at_level(logging.INFO, logger="app.host.turns"):
        await turns.take(_source("nudge", Coalesce.DROP_WHILE_ACTIVE), Signal(at=T0))

    (line,) = [r.getMessage() for r in caplog.records if r.name == "app.host.turns"]
    assert "chinagi" in line and "round" in line and "nudge" in line
    assert "nudge for chinagi" in line


# ---------------------------------------------------------------------------
# JOIN_NEXT
# ---------------------------------------------------------------------------


async def test_signals_that_come_before_the_batch_is_closed_join_it_and_share_its_result():
    turns = _turns("world")
    holder, inside, release = _holding()
    inbox = _source("world-inbox", Coalesce.JOIN_NEXT)

    held = asyncio.create_task(turns.take(holder, Signal(at=T0)))
    await inside.wait()
    deliveries = [
        asyncio.create_task(turns.take(inbox, Signal(at=T0, key=key)))
        for key in ("m1", "m2", "m3")
    ]
    await _settle()
    release.set()
    await held

    results = await asyncio.gather(*deliveries)
    assert results[0] is results[1] is results[2], (
        "the joiners did not get the leader's result"
    )
    assert [s.key for s in results[0].signals] == ["m1", "m2", "m3"]


async def test_a_signal_that_comes_while_due_has_not_closed_the_batch_joins_it_and_shares_its_failure():
    """world asks messaging which handled messages are settled before it closes the batch; a
    delivery that comes during that query is part of this round, and fails with it."""
    turns = _turns("world")
    asking, answered = asyncio.Event(), asyncio.Event()

    async def due(participant: str, batch: Batch, clock) -> _Due:
        asking.set()
        await answered.wait()
        return _Due(reason="the round", signals=tuple(batch.close()))

    async def fails(d: _Due) -> None:
        raise RuntimeError(f"the round of {len(d.signals)} failed")

    inbox = _source("world-inbox", Coalesce.JOIN_NEXT, due=due, run=fails)
    leader = asyncio.create_task(turns.take(inbox, Signal(at=T0, key="m1")))
    await asking.wait()
    joiner = asyncio.create_task(turns.take(inbox, Signal(at=T0, key="m2")))
    await _settle()
    answered.set()

    with pytest.raises(RuntimeError, match="the round of 2 failed"):
        await leader
    with pytest.raises(JoinedTurnFailed) as failed:
        await joiner
    assert isinstance(failed.value.__cause__, RuntimeError)
    assert str(failed.value.__cause__) == "the round of 2 failed"


async def test_a_due_that_raises_fails_the_signals_that_joined_its_batch():
    turns = _turns("world")
    asking, answered = asyncio.Event(), asyncio.Event()

    async def due(participant: str, batch: Batch, clock) -> _Due:
        asking.set()
        await answered.wait()
        raise OSError("the volume cannot be read")

    inbox = _source("world-inbox", Coalesce.JOIN_NEXT, due=due)
    leader = asyncio.create_task(turns.take(inbox, Signal(at=T0, key="m1")))
    await asking.wait()
    joiner = asyncio.create_task(turns.take(inbox, Signal(at=T0, key="m2")))
    await _settle()
    answered.set()

    with pytest.raises(OSError):
        await leader
    with pytest.raises(JoinedTurnFailed) as failed:
        await joiner
    assert isinstance(failed.value.__cause__, OSError)


async def test_a_signal_that_comes_after_the_batch_is_closed_leads_the_next_batch():
    turns = _turns("world")
    closed, finish = asyncio.Event(), asyncio.Event()
    rounds: list[list[str]] = []

    async def due(participant: str, batch: Batch, clock) -> _Due:
        signals = tuple(batch.close())
        rounds.append([s.key for s in signals])
        if len(rounds) == 1:
            closed.set()
            await finish.wait()
        return _Due(reason="the round", signals=signals)

    inbox = _source("world-inbox", Coalesce.JOIN_NEXT, due=due)
    first = asyncio.create_task(turns.take(inbox, Signal(at=T0, key="m1")))
    await closed.wait()
    second = asyncio.create_task(turns.take(inbox, Signal(at=T0, key="m2")))
    third = asyncio.create_task(turns.take(inbox, Signal(at=T0, key="m3")))
    await _settle()
    finish.set()

    assert [s.key for s in (await first).signals] == ["m1"]
    assert [s.key for s in (await second).signals] == ["m2", "m3"]
    assert (await third) is (await second)
    assert rounds == [["m1"], ["m2", "m3"]]


async def test_a_batch_due_did_not_close_is_closed_before_run():
    """A signal that comes while ``run`` runs belongs to the next batch, closed or not by due."""
    turns = _turns("world")
    running, finish = asyncio.Event(), asyncio.Event()
    batches: list[Batch] = []

    async def does_not_close(participant: str, batch: Batch, clock) -> _Due:
        batches.append(batch)
        return _Due(reason="the round")

    async def run(d: _Due) -> int:
        if len(batches) == 1:
            running.set()
            await finish.wait()
        return len(batches)

    inbox = _source("world-inbox", Coalesce.JOIN_NEXT, due=does_not_close, run=run)
    first = asyncio.create_task(turns.take(inbox, Signal(at=T0, key="m1")))
    await running.wait()
    second = asyncio.create_task(turns.take(inbox, Signal(at=T0, key="m2")))
    await _settle()
    finish.set()

    assert (await first, await second) == (1, 2)
    assert [[s.key for s in b.close()] for b in batches] == [["m1"], ["m2"]]


async def test_close_hands_back_the_same_signals_every_time():
    turns = _turns("world")
    seen: list[tuple] = []

    async def due(participant: str, batch: Batch, clock) -> _Due:
        seen.append(tuple(batch.close()))
        seen.append(tuple(batch.close()))
        return _Due(reason="the round")

    await turns.take(
        _source("world-inbox", Coalesce.JOIN_NEXT, due=due), Signal(at=T0, key="m1")
    )
    assert seen[0] == seen[1] == (Signal(at=T0, key="m1"),)


async def test_when_due_says_no_the_joiners_get_no_turn_either():
    turns = _turns("world")
    holder, inside, release = _holding()

    async def says_no(participant: str, batch: Batch, clock) -> None:
        batch.close()
        return None

    inbox = _source("world-inbox", Coalesce.JOIN_NEXT, due=says_no)
    held = asyncio.create_task(turns.take(holder, Signal(at=T0)))
    await inside.wait()
    deliveries = [
        asyncio.create_task(turns.take(inbox, Signal(at=T0, key=key)))
        for key in ("m1", "m2")
    ]
    await _settle()
    release.set()
    await held

    assert await asyncio.gather(*deliveries) == [None, None]


async def test_a_leader_cancelled_while_it_waits_for_the_queue_fails_its_joiners_instead_of_leaving_them():
    turns = _turns("world")
    holder, inside, release = _holding()
    inbox = _source("world-inbox", Coalesce.JOIN_NEXT)

    held = asyncio.create_task(turns.take(holder, Signal(at=T0)))
    await inside.wait()
    leader = asyncio.create_task(turns.take(inbox, Signal(at=T0, key="m1")))
    await _settle()
    joiner = asyncio.create_task(turns.take(inbox, Signal(at=T0, key="m2")))
    await _settle()
    leader.cancel()

    async with asyncio.timeout(1.0):
        with pytest.raises(JoinedTurnFailed) as failed:
            await joiner
    assert isinstance(failed.value.__cause__, asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await leader

    after = asyncio.create_task(turns.take(inbox, Signal(at=T0, key="m3")))
    release.set()
    await held
    assert [s.key for s in (await after).signals] == ["m3"], (
        "a later signal joined the dead batch"
    )


async def test_a_leader_cancelled_in_due_before_it_closed_the_batch_fails_its_joiners():
    turns = _turns("world")
    asking = asyncio.Event()

    async def due(participant: str, batch: Batch, clock) -> _Due:
        asking.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    inbox = _source("world-inbox", Coalesce.JOIN_NEXT, due=due)
    leader = asyncio.create_task(turns.take(inbox, Signal(at=T0, key="m1")))
    await asking.wait()
    joiner = asyncio.create_task(turns.take(inbox, Signal(at=T0, key="m2")))
    await _settle()
    leader.cancel()

    async with asyncio.timeout(1.0):
        with pytest.raises(JoinedTurnFailed):
            await joiner
    with pytest.raises(asyncio.CancelledError):
        await leader
    assert [
        s.key
        for s in (
            await turns.take(
                _source("world-inbox", Coalesce.JOIN_NEXT), Signal(at=T0, key="m3")
            )
        ).signals
    ] == ["m3"]


# ---------------------------------------------------------------------------
# what serial.hold guaranteed (tests/living/test_serial.py, carried over)
# ---------------------------------------------------------------------------


class _ExplodingEngine:
    """Anyone taking a connection blows up: the queue must not hold a database connection."""

    def connect(self):
        raise AssertionError(
            "Turns took a database connection: a turn holds the queue for a whole round, model "
            "calls included, and holding a business connection that long starves the pool"
        )

    def begin(self):
        return self.connect()


def _interval(name: str, seen: list, work: float) -> WakeSource:
    async def run(due: _Due) -> None:
        seen.append(("enter", name))
        await asyncio.sleep(work)
        seen.append(("leave", name))

    return _source(name, Coalesce.QUEUE_EACH, run=run)


async def test_the_queue_does_not_take_a_database_connection(monkeypatch):
    from app.data import session as session_mod

    monkeypatch.setattr(session_mod, "engine", _ExplodingEngine())

    assert await _turns().take(
        _source("regular", Coalesce.DROP_WHILE_ACTIVE), Signal(at=T0)
    )


async def test_the_queue_still_takes_turns_one_at_a_time_without_a_database(
    monkeypatch,
):
    from app.data import session as session_mod

    monkeypatch.setattr(session_mod, "engine", _ExplodingEngine())
    turns, seen = _turns(), []

    await asyncio.gather(
        turns.take(_interval("regular", seen, 0.05), Signal(at=T0)),
        turns.take(_interval("nudge", seen, 0.01), Signal(at=T0)),
    )

    assert [kind for kind, _ in seen] == ["enter", "leave", "enter", "leave"], seen


async def test_a_second_turn_waits_for_the_first_instead_of_being_dropped():
    turns, seen = _turns(), []

    await asyncio.gather(
        turns.take(_interval("regular", seen, 0.30), Signal(at=T0)),
        turns.take(_interval("nudge", seen, 0.05), Signal(at=T0)),
    )

    assert seen == [
        ("enter", "regular"),
        ("leave", "regular"),
        ("enter", "nudge"),
        ("leave", "nudge"),
    ]


async def test_two_people_do_not_block_each_other():
    """One person cannot think two things at once; two people can."""
    seen: list = []

    await asyncio.gather(
        _turns("akao").take(_interval("akao", seen, 0.30), Signal(at=T0)),
        _turns("ayana").take(_interval("ayana", seen, 0.05), Signal(at=T0)),
    )

    assert [kind for kind, _ in seen] == ["enter", "enter", "leave", "leave"], seen
    assert [name for kind, name in seen if kind == "leave"] == ["ayana", "akao"]


async def test_the_queue_is_let_go_when_a_turn_raises():
    turns = _turns()

    async def boom(due: _Due) -> None:
        raise LookupError("boom")

    with pytest.raises(LookupError):
        await turns.take(
            _source("regular", Coalesce.QUEUE_EACH, run=boom), Signal(at=T0)
        )
    async with asyncio.timeout(1.0):
        assert await turns.take(_source("regular", Coalesce.QUEUE_EACH), Signal(at=T0))


async def test_the_queue_is_let_go_when_a_turn_never_returns():
    """2026-09-16 on prod: one call that hung (no answer, no timeout) stopped akao and chinagi for
    15 hours; the process alive, other keys running, no log, no trace, no error. Letting go on an
    error does not cover a turn that never ends; the hold limit does."""
    turns = _turns(hold_limit=timedelta(seconds=0.05))

    async def hangs(due: _Due) -> None:
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await turns.take(
            _source("regular", Coalesce.QUEUE_EACH, run=hangs), Signal(at=T0)
        )
    async with asyncio.timeout(1.0):
        assert await turns.take(_source("regular", Coalesce.QUEUE_EACH), Signal(at=T0))


async def test_the_next_turn_gets_through_after_a_hung_one_is_cut_off():
    turns = _turns(hold_limit=timedelta(seconds=0.05))
    seen: list[str] = []

    async def hangs(due: _Due) -> None:
        seen.append("hung-enter")
        await asyncio.Event().wait()

    async def next_one(due: _Due) -> None:
        seen.append("next-enter")

    async def hung() -> None:
        with pytest.raises(TimeoutError):
            await turns.take(
                _source("hung", Coalesce.QUEUE_EACH, run=hangs), Signal(at=T0)
            )
        seen.append("hung-timeout")

    async def queued() -> None:
        await asyncio.sleep(0.01)
        await turns.take(
            _source("next", Coalesce.QUEUE_EACH, run=next_one), Signal(at=T0)
        )

    async with asyncio.timeout(2.0):
        await asyncio.gather(hung(), queued())

    assert seen == ["hung-enter", "hung-timeout", "next-enter"]


async def test_a_turn_inside_the_hold_limit_is_not_cut_short():
    """The limit catches a turn that never ends; it is not a budget for her rounds."""
    turns = _turns(hold_limit=timedelta(seconds=1))

    async def takes_a_while(due: _Due) -> str:
        await asyncio.sleep(0.05)
        return "finished"

    assert (
        await turns.take(
            _source("regular", Coalesce.QUEUE_EACH, run=takes_a_while), Signal(at=T0)
        )
        == "finished"
    )


async def test_a_timeout_from_inside_the_turn_is_not_blamed_on_the_hold_limit(caplog):
    """A turn that raises a TimeoutError of its own (an HTTP timeout) did not hit the hold limit;
    a log line saying it held the queue too long would point at the wrong thing."""
    turns = _turns(hold_limit=timedelta(seconds=60))

    async def model_timed_out(due: _Due) -> None:
        raise TimeoutError("the model timed out, not the queue")

    with caplog.at_level(logging.WARNING, logger="app.host.turns"):
        with pytest.raises(TimeoutError):
            await turns.take(
                _source("regular", Coalesce.QUEUE_EACH, run=model_timed_out),
                Signal(at=T0),
            )

    assert "held the queue" not in caplog.text, caplog.text


async def test_an_inner_queue_hitting_its_limit_is_not_blamed_on_the_outer(caplog):
    outer = _turns("akao", "round", hold_limit=timedelta(seconds=60))
    inner = _turns("akao", "inner", hold_limit=timedelta(seconds=0.05))

    async def hangs(due: _Due) -> None:
        await asyncio.Event().wait()

    async def takes_the_inner(due: _Due) -> None:
        await inner.take(
            _source("inner", Coalesce.QUEUE_EACH, run=hangs), Signal(at=T0)
        )

    with caplog.at_level(logging.WARNING, logger="app.host.turns"):
        with pytest.raises(TimeoutError):
            await outer.take(
                _source("outer", Coalesce.QUEUE_EACH, run=takes_the_inner),
                Signal(at=T0),
            )

    assert "akao/inner held the queue" in caplog.text, caplog.text
    assert "akao/round held the queue" not in caplog.text, caplog.text


async def test_a_cancellation_from_outside_is_not_reported_as_hitting_the_limit_and_lets_go(
    caplog,
):
    """A stop cancels the turn: the cancellation goes up as it is, and the queue is free again."""
    turns = _turns(hold_limit=timedelta(seconds=60))
    started = asyncio.Event()

    async def waits(due: _Due) -> None:
        started.set()
        await asyncio.Event().wait()

    with caplog.at_level(logging.WARNING, logger="app.host.turns"):
        task = asyncio.create_task(
            turns.take(
                _source("regular", Coalesce.QUEUE_EACH, run=waits), Signal(at=T0)
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert "held the queue" not in caplog.text, caplog.text
    async with asyncio.timeout(1.0):
        assert await turns.take(_source("regular", Coalesce.QUEUE_EACH), Signal(at=T0))


def test_whoever_makes_a_queue_says_whether_it_has_a_hold_limit():
    """The cap used to apply when nobody passed one; the call sites that hung on 2026-09-16 passed
    none. A queue has no default either way: leaving it out is an error, not a queue without a cap."""
    with pytest.raises(TypeError):
        Turns("akao", "round", clock=_stopped_at())  # type: ignore[call-arg]
