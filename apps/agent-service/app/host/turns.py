"""Turns: one participant's queue of turns on one line.

A participant's rounds run one at a time on a queue. A participant can have several queues, one
per line, and they do not block each other: her rounds (the regular rhythm and the early wakes),
her diary and her weekly look back each have one; world's rounds have one.

**A signal asks for a turn; the wake source decides what the turn is.** A signal is a clock tick or
a delivery (:class:`Signal`). It carries no work: once its turn comes, the source's ``due`` looks
at what is true then (stored state, the signals of its batch) and says whether a round is due and
which one; the source's ``run`` runs it. Deciding at the turn and not when the signal came is what
keeps a tick that waited behind a stuck round from running with what was true fifteen minutes ago.

**How signals of one source coalesce** is the source's to declare (:class:`Coalesce`), and there
are the three ways that exist today: every signal queued (her diary, her weekly look back); a
signal dropped while the same source already has one queued or running (her regular rhythm, her
early wakes: one tick waiting behind a stuck round is enough, it decides on what is true when it
gets its turn); signals joining the next batch until that batch's ``due`` closes it, sharing its
result (world's inbox: what arrives while a round runs is taken by the next round together).
Different sources never coalesce; on one queue they take turns first come, first served.

**Where the batch closes and when the clock is read are the source's.** ``due`` gets the batch and
the clock. world's inbox asks messaging which handled messages are settled before it closes its
batch, so a delivery that arrives during that query is part of this round and fails with it; her
sources read the clock first thing, before reading any state. A batch ``due`` left open is closed
before ``run``.

**Turns starts no task and never reads the clock.** A turn runs in the task of the caller whose
signal leads it (a clock's work task, an inbox handler's task), so stopping is what it is for that
caller: a clock stops by cancelling its work at once, an inbox gives a handler twenty seconds. A
leader that is cancelled or fails before its batch is closed hands that to the signals that joined
it (:class:`JoinedTurnFailed`), so they never wait for a turn nobody will run.

**One process.** The queue is an asyncio lock: turns are mutually exclusive within this process,
not across processes. Two processes of a rolling deploy can each run a turn of the same participant
at the same time; what the database deduplicates (a round landing once, a send claimed once) does
not undo a model call or a send that already happened. Today's risk, unchanged by this queue.

Not a Postgres advisory lock, which her rounds used before: a session lock held for a whole turn
(model calls of tens of seconds included) takes a business connection for the holder and one for
each waiter, so a backlog drains the pool and the holder itself fails for want of a connection; and
a session lock is released silently when its connection drops during a model call while the turn
goes on, so a second turn enters and both run, unseen. An asyncio lock is let go only when the turn
holding it ends. A turn must not take a turn on its own queue: the lock is not reentrant, and it
would wait for itself until the hold limit.

**hold_limit.** A turn that holds the queue longer than this is cut off with ``TimeoutError`` in
the holder, and the queue moves on. It is not a budget for a round: it turns "never ends" into a
crash, which every round already recovers from (2026-09-16 on prod: one call that hung, with no
answer and no timeout, stopped two of the sisters for fifteen hours, silently). The limit covers
``due`` and ``run`` and starts when the turn comes, not while the signal waits. A turn cut off is
not left clean: a send whose outcome is unknown is settled by the reconciliation that already
exists. Cancellation is cooperative, so a turn that spins without awaiting, or that swallows the
cancellation and returns, is not cut off (the queue is let go when it returns). The limit does not
replace the timeouts below it (the model call, HTTP, the database); it keeps the queue from being
held forever whichever of them is missing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Generic, TypeVar

from app.agent.contracts import Due

logger = logging.getLogger(__name__)

D = TypeVar("D", bound=Due)
R = TypeVar("R")

Clock = Callable[[], datetime]


class Coalesce(Enum):
    """How the signals of one source coalesce on one queue."""

    # Every signal is queued and gets a turn of its own (her diary, her weekly look back).
    QUEUE_EACH = "queue_each"
    # While this source has a signal queued or running, another one is dropped: ``take`` returns
    # ``None`` at once, without waiting (her regular rhythm, her early wakes).
    DROP_WHILE_ACTIVE = "drop_while_active"
    # Signals that come before the next batch is closed join it and share its result, failures
    # included; the first one leads the batch (world's inbox).
    JOIN_NEXT = "join_next"


@dataclass(frozen=True)
class Signal:
    """A request for a turn."""

    # When the signal came: the clock's tick, the delivery's arrival.
    at: datetime
    # With JOIN_NEXT, which of the waiting parties this is (world: the message id).
    key: str | None = None


class Batch:
    """The signals a turn is taken for.

    With QUEUE_EACH and DROP_WHILE_ACTIVE it holds the one signal and is closed when ``due`` gets
    it. With JOIN_NEXT it stays open until :meth:`close`: signals of the same source that come
    before that join it, the ones after start the next batch.
    """

    def __init__(self, first: Signal) -> None:
        self._signals: list[Signal] = [first]
        self._closed: tuple[Signal, ...] | None = None
        self._on_close: Callable[[], None] | None = None

    def close(self) -> Sequence[Signal]:
        """Close the batch and hand back every signal in it, in the order they came. Calling it
        again hands back the same signals."""
        if self._closed is None:
            self._closed = tuple(self._signals)
            if self._on_close is not None:
                self._on_close()
        return self._closed


@dataclass(frozen=True)
class WakeSource(Generic[D, R]):
    """Where one kind of signal comes from, how it coalesces, and what a turn for it does."""

    # "regular" / "nudge" / "day-page" / "persona-review" / "world-inbox"
    name: str
    coalesce: Coalesce
    # (participant, the batch, the clock) -> the round to run, or None for none. Called once the
    # queue is the batch's. It reads stored state and the signals; it may write a record of the
    # identity it decided on (her early wake writes that its round began).
    due: Callable[[str, Batch, Clock], Awaitable[D | None]]
    run: Callable[[D], Awaitable[R]]


class JoinedTurnFailed(Exception):
    """What a signal that joined a batch gets when the turn did not finish; ``__cause__`` is the
    leader's error, a failure of ``due`` or ``run`` or the leader's cancellation."""


@dataclass(eq=False)
class _Open:
    """A JOIN_NEXT batch still taking signals, and the outcome the ones that joined it wait for."""

    batch: Batch
    done: asyncio.Event = field(default_factory=asyncio.Event)
    result: object = None
    failure: BaseException | None = None


class Turns:
    """One participant's queue of turns on one line. See the module docstring."""

    def __init__(
        self,
        participant: str,
        line: str,
        *,
        hold_limit: timedelta | None,
        clock: Clock,
    ) -> None:
        self.participant = participant
        self.line = line
        self.hold_limit = hold_limit
        self._clock = clock
        self._queue = asyncio.Lock()
        # Sources with DROP_WHILE_ACTIVE that have a signal queued or running.
        self._active: set[str] = set()
        # Sources with JOIN_NEXT whose next batch is still open.
        self._open: dict[str, _Open] = {}

    def __repr__(self) -> str:
        return f"Turns({self.participant}/{self.line})"

    async def take(self, source: WakeSource[D, R], signal: Signal) -> R | None:
        """Take a turn for ``signal`` as ``source`` coalesces it; once the turn comes, ask
        ``source.due`` and, when a round is due, log its reason and run it.

        Returns what ``run`` returned; ``None`` when the signal was dropped or ``due`` decided no
        round is due. ``due``'s and ``run``'s errors are raised as they are to the signal leading
        the turn and as :class:`JoinedTurnFailed` to the ones that joined it. ``hold_limit`` raises
        ``TimeoutError`` in the leader.
        """
        if source.coalesce is Coalesce.JOIN_NEXT:
            return await self._join_next(source, signal)
        batch = Batch(signal)
        batch.close()
        if source.coalesce is Coalesce.QUEUE_EACH:
            return await self._turn(source, batch)
        if source.name in self._active:
            return None
        self._active.add(source.name)
        try:
            return await self._turn(source, batch)
        finally:
            self._active.discard(source.name)

    async def _join_next(self, source: WakeSource[D, R], signal: Signal) -> R | None:
        joining = self._open.get(source.name)
        if joining is not None:
            joining.batch._signals.append(signal)
            await joining.done.wait()
            if joining.failure is not None:
                failure = joining.failure
                raise JoinedTurnFailed(
                    f"the turn this signal joined did not finish: "
                    f"{type(failure).__name__}: {failure}"
                ) from failure
            return joining.result  # type: ignore[return-value]

        leading = _Open(Batch(signal))
        self._open[source.name] = leading

        def stop_joining() -> None:
            if self._open.get(source.name) is leading:
                del self._open[source.name]

        leading.batch._on_close = stop_joining
        try:
            leading.result = await self._turn(source, leading.batch)
            return leading.result
        except BaseException as exc:
            leading.failure = exc
            raise
        finally:
            leading.batch.close()
            leading.done.set()

    async def _turn(self, source: WakeSource[D, R], batch: Batch) -> R | None:
        async with self._queue:
            limit = None if self.hold_limit is None else self.hold_limit.total_seconds()
            try:
                async with asyncio.timeout(limit) as cutoff:
                    due = await source.due(self.participant, batch, self._clock)
                    batch.close()
                    if due is None:
                        return None
                    logger.info(
                        "turns: %s/%s %s: %s",
                        self.participant,
                        self.line,
                        source.name,
                        due.reason,
                    )
                    return await source.run(due)
            except TimeoutError:
                # Only this queue's own limit counts. A TimeoutError the turn raised itself (an HTTP
                # timeout) or an inner queue's limit passes through here too; reporting those as
                # this queue held too long would point at the wrong thing.
                if cutoff.expired():
                    logger.warning(
                        "turns: %s/%s held the queue for more than %.0f s; the turn is cut off "
                        "and handled as a crash, the queue moves on",
                        self.participant,
                        self.line,
                        limit,
                    )
                raise
