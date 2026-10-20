"""Living: the three sisters' life engine — for each sister a queue of rounds with two wakes on it
and a queue each for her diary and her weekly look back with one, a clock, the reading inbox, the
two outbound queues, the sisters' inboxes, and a one-off admin route that empties the old reading
queue.

  each sister's queue of rounds ("round", ``ctx.turns``), signalled by two wakes (``ctx.wake``):
  every  60 s  regular   -> regular_moment_due / run_due_moment   her regular rhythm
  every  60 s  nudge     -> nudged_moment_due / run_due_moment    wake her early for a call or a message

  each sister's diary ("day-page") and weekly look back ("persona-review"), one wake each:
  every 300 s  day-page        -> day_page_due / write_due_page          yesterday's page (04:00-06:00)
  every 300 s  persona-review  -> persona_review_due / write_due_review  last week's "who I am" (06:00-08:00)

  every 300 s  LandingTick  -> landing_tick  match what she said with what landed

  ChatResponseSegment -> chat_response_{channel}          what she says
  Recall              -> recall_{channel}                 what she takes back

  the inboxes named after the three personas -> received  stored only; she reads them on her clock
  the reading inbox                          -> read_a_round  one stretch of reading

  POST /admin/reading/carry-over  carry_over_reading  the old reading queue -> the reading inbox

**Her rounds take turns on one queue.** One person cannot think two things at once: her regular
rhythm and her early wakes share her queue, a turn at a time, first come first served. Each of the
two drops a tick while it already has one queued or running (``DROP_WHILE_ACTIVE``): one tick
waiting behind a stuck round is enough, since it decides on what is true when it gets its turn.
A turn holds her queue for at most :data:`app.living.serial.HELD_SECONDS`; a turn that never ends
is cut off there and handled as a crash. Each tick reads the database and compares; most run
nothing. Her diary and her weekly look back do not take turns on this queue.

**Her diary and her weekly look back have a queue each** ("day-page", "persona-review"), with the
same hold limit, so writing yesterday's page or rewriting who she is never keeps her from living
today, nor each other. Every tick of their wakes is queued (``QUEUE_EACH``): a tick waiting behind a
stuck one gets its turn, and decides then whether there is still something to write. Whether a tick
counts is judged by when it ticked (the window), what it writes by when it got its turn
(:func:`app.living.day_page.day_page_due`, :func:`app.living.persona_review.persona_review_due`).

**Clocks tick denser than the work they drive.** A clock's seconds are fixed when it is
registered, so the intervals that are business parameters (ten minutes between rounds, the
diary and review windows) are judged on every tick, in the wake's ``due`` or in the node; most
ticks run nothing. Her early wake ticks every minute so that someone calling her waits at most a
minute.

**A tick builds its payload in the clock loop** (:func:`_ticker`): a tick Data that grew a
required field fails there, and the clock's watchdog stops the process. The node's run is the
tick's work and runs on its own.

**She has no inbound edge.** Messages on her phone she reads from ``common_message`` in her own
rounds; what world and her sisters tell her arrives in her inbox, which only stores it
(``tests/living/test_no_inbound.py``).

**The reading inbox** (:data:`app.living.participants.READING`) gets a message when she picks a
file up in one of her rounds (:func:`app.living.reading.read_a_bit`); its handler reads that
stretch. A message is handled at most once (``max_attempts=1``): a stretch costs model calls, so
an error past the stretch dead-letters the message and a person decides whether to replay it.
Processing is limited to five minutes, above the reading agent's own timeout; it bounds how long a
stretch whose process died stays claimed before another process reads it again. What reaches this
inbox from outside can run the reading agent, not her round (``tests/living/test_no_inbound.py``).

**The old reading queue.** Reading used to run from a durable edge whose queue
(``durable_file_picked_up_read_a_round``) nothing consumes any more. The admin route reports what is
left there, carries the rounds that were never read over to the reading inbox, and deletes the
lane's empty queue (:mod:`app.nodes.reading_carry_over`). Its handler is not in ``app.living``; it
has the inner credential and lane checks of the operator routes. It is removed once prod has
switched over and the old queue is deleted.

**The inboxes are opened at start**: their names come from the persona table, which cannot be read
at import (:func:`app.living.received.open_inboxes`, which declares them through the function the
host hands it).

**``records`` and ``pictures`` are imported for their Data classes.** A Data class registers when
its module is imported, and the schema step builds tables only for registered classes. Both are
also reached through ``moment`` today; importing them here keeps their tables from depending on
that chain. The other living Data classes come in with the modules below.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from app.data.schema import Data
from app.domain.chat_dataflow import ChatResponseSegment
from app.domain.safety import Recall
from app.host import Batch, Coalesce, Context, Plugin, Tick, WakeSource
from app.living import pictures, records  # noqa: F401
from app.living.day_page import DuePage, LivingDayPage, day_page_due, write_due_page
from app.living.landing import LANDING_TICK_SECONDS, LandingTick, landing_tick
from app.living.moment import DueMoment, LifeMoment, regular_moment_due, run_due_moment
from app.living.nudge import nudged_moment_due
from app.living.participants import READING
from app.living.persona import LIVING_PERSONAS, PersonaVersion
from app.living.persona_review import DueReview, persona_review_due, write_due_review
from app.living.reading import read_a_round
from app.living.received import open_inboxes
from app.living.records import living_lane
from app.living.serial import HELD_SECONDS
from app.nodes.reading_carry_over import ReadingCarryOverRequest, carry_over_reading

# How often her wakes tick (see "Clocks tick denser than the work they drive").
REGULAR_EVERY = timedelta(seconds=60)
NUDGE_EVERY = timedelta(seconds=60)
# Five minutes: 24 tries inside the two hours of the diary's and the look back's windows; a tick
# that fails (the model timed out, she wrote nothing) is tried again by the next one.
DAY_PAGE_EVERY = timedelta(minutes=5)
PERSONA_REVIEW_EVERY = timedelta(minutes=5)

# How long a turn may hold one of her queues (:mod:`app.host.turns`).
HOLD_LIMIT = timedelta(seconds=HELD_SECONDS)

# (tick Data, seconds, node). The clock is named after its tick Data.
CLOCKS: tuple[tuple[type[Data], float, Callable[[Data], Awaitable[None]]], ...] = (
    (LandingTick, LANDING_TICK_SECONDS, landing_tick),
)


def _ticker(data_type: type[Data], node: Callable[[Data], Awaitable[None]]) -> Tick:
    """Build ``data_type(ts=<iso>)`` now, in the clock loop; hand it to ``node`` as the work."""

    def tick(ts: datetime) -> Awaitable[None]:
        return node(data_type(ts=ts.isoformat()))

    return tick


def regular(lane: str) -> WakeSource[DueMoment, LifeMoment]:
    """Her regular rhythm on ``lane``: a round when her current cell has none
    (:func:`app.living.moment.regular_moment_due`)."""

    async def due(persona_id: str, batch: Batch, clock: Callable[[], datetime]) -> DueMoment | None:
        return await regular_moment_due(lane=lane, persona_id=persona_id, clock=clock)

    return WakeSource("regular", Coalesce.DROP_WHILE_ACTIVE, due, run_due_moment)


def nudge(lane: str) -> WakeSource[DueMoment, LifeMoment]:
    """Her early wakes on ``lane``: a round when someone calls her on her phone or a message for
    her wants to wake her (:func:`app.living.nudge.nudged_moment_due`)."""

    async def due(persona_id: str, batch: Batch, clock: Callable[[], datetime]) -> DueMoment | None:
        return await nudged_moment_due(lane=lane, persona_id=persona_id, clock=clock)

    return WakeSource("nudge", Coalesce.DROP_WHILE_ACTIVE, due, run_due_moment)


def day_page(lane: str) -> WakeSource[DuePage, LivingDayPage | None]:
    """Her diary on ``lane``: the page of the living day that just ended, when the tick came in the
    window and the day has no page yet (:func:`app.living.day_page.day_page_due`)."""

    async def due(persona_id: str, batch: Batch, clock: Callable[[], datetime]) -> DuePage | None:
        (tick,) = batch.close()
        return await day_page_due(lane=lane, persona_id=persona_id, at=tick.at, clock=clock)

    return WakeSource("day-page", Coalesce.QUEUE_EACH, due, write_due_page)


def persona_review(lane: str) -> WakeSource[DueReview, PersonaVersion | None]:
    """Her weekly look back on ``lane``: a new "who I am" from last week's pages, when the tick
    came in the window and this week has no review yet
    (:func:`app.living.persona_review.persona_review_due`)."""

    async def due(persona_id: str, batch: Batch, clock: Callable[[], datetime]) -> DueReview | None:
        (tick,) = batch.close()
        return await persona_review_due(lane=lane, persona_id=persona_id, at=tick.at, clock=clock)

    return WakeSource("persona-review", Coalesce.QUEUE_EACH, due, write_due_review)


def setup(ctx: Context) -> None:
    lane = living_lane()
    regular_rounds, early_wakes = regular(lane), nudge(lane)
    pages, reviews = day_page(lane), persona_review(lane)
    for persona_id in LIVING_PERSONAS:
        rounds = ctx.turns(persona_id, "round", hold_limit=HOLD_LIMIT)
        ctx.wake(rounds, regular_rounds, every=REGULAR_EVERY)
        ctx.wake(rounds, early_wakes, every=NUDGE_EVERY)
        diary = ctx.turns(persona_id, "day-page", hold_limit=HOLD_LIMIT)
        ctx.wake(diary, pages, every=DAY_PAGE_EVERY)
        look_back = ctx.turns(persona_id, "persona-review", hold_limit=HOLD_LIMIT)
        ctx.wake(look_back, reviews, every=PERSONA_REVIEW_EVERY)
    for data_type, seconds, node in CLOCKS:
        ctx.clock(data_type.__name__, seconds, _ticker(data_type, node))
    ctx.inbox(
        READING,
        on_message=read_a_round,
        processing_timeout=timedelta(minutes=5),
        max_attempts=1,
    )
    ctx.route(
        "POST",
        "/admin/reading/carry-over",
        ReadingCarryOverRequest,
        carry_over_reading,
        inner_secret=True,
        lane_match=True,
        answers_with_lane=True,
    )
    ctx.outbound(ChatResponseSegment, "chat_response")
    ctx.outbound(Recall, "recall")
    ctx.inboxes_at_start(open_inboxes)


PLUGIN = Plugin(name="living", setup=setup, requires=("skills",))
