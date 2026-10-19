"""The host's queues of turns (``ctx.turns``) and the clocks that signal them (``ctx.wake``).

``ctx.turns`` registers a participant's queue on one line (``app.host.turns``); ``ctx.wake`` signals
a queue for a source every interval. What these tests pin:

* one queue per participant and line;
* registrations of one source with one interval share one clock, which signals every participant
  at once on each tick; one participant's error is logged and does not keep the others from their
  turn;
* a clock-driven wake is a clock: outside prod it does not run without
  ``DATAFLOW_ENABLE_TIME_SOURCES=1``; a queue someone signals by delivery runs whatever the lane;
* the first signal of a clock-driven wake comes no sooner than one interval after the clock
  starts (within this process; nothing here says anything about another process);
* a stop cancels the turns its clocks are running before it stops messaging;
* ``registered()`` shows the queues and the wakes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from app.host import Coalesce, Host, HostError, Signal, Turns, WakeSource

from .conftest import plugin

QUICK = timedelta(seconds=0.05)


@pytest.fixture(autouse=True)
def _prod_lane(monkeypatch):
    monkeypatch.delenv("LANE", raising=False)
    monkeypatch.delenv("DATAFLOW_ENABLE_TIME_SOURCES", raising=False)


@dataclass(frozen=True)
class _Due:
    reason: str
    participant: str
    at: datetime


def _source(name: str = "regular", *, due=None, run=None) -> WakeSource:
    async def always(participant: str, batch, clock) -> _Due:
        (signal,) = batch.close()
        return _Due(reason=f"{name} tick", participant=participant, at=signal.at)

    async def nothing(due: _Due) -> None:
        return None

    return WakeSource(name, Coalesce.DROP_WHILE_ACTIVE, due or always, run or nothing)


def _wakes(*participants: str, source: WakeSource, every: timedelta = QUICK) -> Host:
    def setup(ctx) -> None:
        for participant in participants:
            ctx.wake(ctx.turns(participant, hold_limit=None), source, every=every)

    return Host("agent-service", [plugin("p", setup)])


async def _start(host: Host) -> None:
    await host.start(http=None, schema=False, mq=False, clocks=True, tasks=False)


def _clock_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("clock[")]


# ---------------------------------------------------------------------------
# ctx.turns
# ---------------------------------------------------------------------------


async def test_a_participant_has_one_queue_per_line():
    def setup(ctx) -> None:
        ctx.turns("akao", "round", hold_limit=None)
        ctx.turns("akao", "day-page", hold_limit=None)
        ctx.turns("ayana", "round", hold_limit=None)
        ctx.turns("akao", "round", hold_limit=None)

    host = Host("agent-service", [plugin("p", setup)])

    with pytest.raises(HostError, match="turns 'akao/round'"):
        await host.start(http=None, schema=False, mq=False, clocks=False, tasks=False)
    assert host.registered() == ()


async def test_registered_shows_the_queues_and_the_wakes():
    source = _source("regular")
    made: list[Turns] = []

    def setup(ctx) -> None:
        turns = ctx.turns("akao", "round", hold_limit=timedelta(seconds=900))
        made.append(turns)
        ctx.wake(turns, source, every=timedelta(seconds=60))

    host = Host("agent-service", [plugin("living", setup)])
    await host.start(http=None, schema=False, mq=False, clocks=False, tasks=False)
    try:
        by_kind = {r.kind: r for r in host.registered()}
    finally:
        await host.stop()

    (turns,) = made
    assert (turns.participant, turns.line, turns.hold_limit) == (
        "akao",
        "round",
        timedelta(seconds=900),
    )
    assert by_kind["turns"].plugin == "living"
    assert by_kind["turns"].name == "akao/round"
    assert by_kind["turns"].detail == {"turns": turns}
    assert by_kind["wake"].name == "regular -> akao/round"
    assert by_kind["wake"].detail == {
        "turns": turns,
        "source": source,
        "every": timedelta(seconds=60),
    }


async def test_a_queue_signalled_by_delivery_runs_in_a_lane_whose_clocks_are_off(
    monkeypatch,
):
    """world's inbox signals its queue from the inbox handler, not from a clock; the switch for time
    sources is not its business."""
    monkeypatch.setenv("LANE", "coe-test")
    made: list[Turns] = []

    def setup(ctx) -> None:
        made.append(ctx.turns("world", hold_limit=None))

    host = Host("world", [plugin("p", setup)])
    await _start(host)
    try:
        ran = await made[0].take(
            _source("world-inbox", run=lambda due: asyncio.sleep(0, result="ran")),
            Signal(at=datetime.now().astimezone(), key="m1"),
        )
    finally:
        await host.stop()

    assert ran == "ran"


# ---------------------------------------------------------------------------
# ctx.wake
# ---------------------------------------------------------------------------


async def test_wakes_of_one_source_and_one_interval_share_one_clock():
    regular = _source("regular")
    nudge = _source("nudge")

    def setup(ctx) -> None:
        for participant in ("akao", "ayana", "chinagi"):
            turns = ctx.turns(participant, hold_limit=None)
            ctx.wake(turns, regular, every=timedelta(seconds=60))
            ctx.wake(turns, nudge, every=timedelta(seconds=60))
        ctx.wake(
            ctx.turns("akao", "day-page", hold_limit=None),
            regular,
            every=timedelta(seconds=300),
        )

    host = Host("agent-service", [plugin("p", setup)])
    await _start(host)
    try:
        assert len(_clock_tasks()) == 3
    finally:
        await host.stop()


async def test_each_tick_signals_every_participant_at_once_with_the_time_it_ticked():
    """A turn of akao's that does not end does not keep ayana and chinagi waiting for theirs."""
    ran: dict[str, list[datetime]] = {}
    stuck = asyncio.Event()

    async def run(due: _Due) -> None:
        ran.setdefault(due.participant, []).append(due.at)
        if due.participant == "akao":
            await stuck.wait()

    host = _wakes("akao", "ayana", "chinagi", source=_source(run=run))
    before = datetime.now().astimezone()
    await _start(host)
    try:
        async with asyncio.timeout(2):
            while len(ran.get("ayana", [])) < 2 or len(ran.get("chinagi", [])) < 2:
                await asyncio.sleep(0.01)
    finally:
        await host.stop()

    assert len(ran["akao"]) == 1, (
        "akao's turn that never ended was signalled into again"
    )
    assert ran["ayana"][0] == ran["akao"][0] == ran["chinagi"][0], "one tick, one time"
    assert ran["ayana"][0] >= before


async def test_one_participant_raising_does_not_keep_the_others_from_their_turn(caplog):
    ran: list[str] = []

    async def due(participant: str, batch, clock) -> _Due:
        if participant == "akao":
            raise RuntimeError("akao's state cannot be read")
        (signal,) = batch.close()
        return _Due(reason="tick", participant=participant, at=signal.at)

    async def run(d: _Due) -> None:
        ran.append(d.participant)

    host = _wakes("akao", "ayana", source=_source(due=due, run=run))
    with caplog.at_level(logging.WARNING, logger="app.host.host"):
        await _start(host)
        try:
            async with asyncio.timeout(2):
                while ran.count("ayana") < 2:
                    await asyncio.sleep(0.01)
        finally:
            await host.stop()

    assert "akao" not in ran
    assert "akao's state cannot be read" in caplog.text
    assert "regular" in caplog.text and "akao/round" in caplog.text


async def test_a_clock_driven_wake_does_not_run_in_a_test_lane_by_default(monkeypatch):
    monkeypatch.setenv("LANE", "coe-test")
    ran: list[str] = []

    async def run(d: _Due) -> None:
        ran.append(d.participant)

    host = _wakes("akao", source=_source(run=run))
    await _start(host)
    try:
        await asyncio.sleep(0.2)
    finally:
        await host.stop()

    assert ran == []


async def test_a_clock_driven_wake_runs_in_a_test_lane_with_time_sources_switched_on(
    monkeypatch,
):
    monkeypatch.setenv("LANE", "coe-test")
    monkeypatch.setenv("DATAFLOW_ENABLE_TIME_SOURCES", "1")
    ran: list[str] = []

    async def run(d: _Due) -> None:
        ran.append(d.participant)

    host = _wakes("akao", source=_source(run=run))
    await _start(host)
    try:
        async with asyncio.timeout(2):
            while not ran:
                await asyncio.sleep(0.01)
    finally:
        await host.stop()


async def test_the_first_signal_comes_no_sooner_than_one_interval_after_the_clock_starts():
    """Within this process the clock's first tick is one interval after it starts, never at once.
    It says nothing about another process: two processes of a rolling deploy can overlap."""
    every = timedelta(seconds=0.3)
    signalled: list[float] = []

    async def run(d: _Due) -> None:
        signalled.append(asyncio.get_running_loop().time())

    host = _wakes("akao", source=_source(run=run), every=every)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await _start(host)
    try:
        async with asyncio.timeout(2):
            while not signalled:
                await asyncio.sleep(0.01)
    finally:
        await host.stop()

    assert signalled[0] - started >= every.total_seconds()


async def test_a_stop_cancels_the_turns_its_clocks_run_before_it_stops_messaging(
    monkeypatch,
):
    """Her round runs in the clock's work: a stop cancels it at once, with no grace, before the
    messaging drain gives the inbox handlers their twenty seconds."""
    import app.host.host as host_module

    events: list[str] = []

    class _Mq:
        async def connect(self) -> None:
            pass

        async def declare_topology(self) -> None:
            pass

        async def close(self) -> None:
            events.append("mq.close")

    async def start_messaging() -> None:
        pass

    async def stop_messaging() -> None:
        events.append("stop_messaging")

    monkeypatch.setattr(host_module, "mq", _Mq())
    monkeypatch.setattr(host_module, "start_messaging", start_messaging)
    monkeypatch.setattr(host_module, "stop_messaging", stop_messaging)
    running = asyncio.Event()

    async def run(d: _Due) -> None:
        running.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("turn cancelled")
            raise

    host = _wakes("akao", source=_source(run=run))
    await host.start(http=None, schema=False, mq=True, clocks=True, tasks=False)
    async with asyncio.timeout(2):
        await running.wait()
    await host.stop()

    assert events == ["turn cancelled", "stop_messaging", "mq.close"]


async def test_a_wake_taken_back_signals_no_more_while_the_others_go_on():
    ran: list[str] = []
    disposers: dict[str, object] = {}

    async def run(d: _Due) -> None:
        ran.append(d.participant)

    source = _source(run=run)

    def setup(ctx) -> None:
        for participant in ("akao", "ayana"):
            disposers[participant] = ctx.wake(
                ctx.turns(participant, hold_limit=None), source, every=QUICK
            )

    host = Host("agent-service", [plugin("p", setup)])
    await _start(host)
    try:
        await disposers["akao"]()
        await asyncio.sleep(
            2 * QUICK.total_seconds()
        )  # a tick already under way finishes
        ran.clear()
        async with asyncio.timeout(2):
            while ran.count("ayana") < 2:
                await asyncio.sleep(0.01)
        assert "akao" not in ran
        assert {r.name for r in host.registered() if r.kind == "wake"} == {
            "regular -> ayana/round"
        }
    finally:
        await host.stop()
