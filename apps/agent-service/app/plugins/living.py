"""Living: the three sisters' life engine — for each sister a queue of rounds with two wakes on it
and a queue each for her diary and her weekly look back with one, the landing clock, the reading
inbox, the two outbound queues, the sisters' inboxes, and a one-off admin route that empties the
old reading queue.

  each sister's queue of rounds ("round", ``ctx.turns``), signalled by two wakes (``ctx.wake``):
  every  60 s  regular   -> regular_moment_due / run_due_moment   her regular rhythm
  every  60 s  nudge     -> nudged_moment_due / run_due_moment    wake her early for a call or a message

  each sister's diary ("day-page") and weekly look back ("persona-review"), one wake each:
  every 300 s  day-page        -> day_page_due / write_due_page          yesterday's page (04:00-06:00)
  every 300 s  persona-review  -> persona_review_due / write_due_review  last week's "who I am" (06:00-08:00)

  every 300 s  landing  -> reconcile_what_she_said  match what she said with what landed

  ChatResponseSegment -> chat_response_{channel}          what she says
  Recall              -> recall_{channel}                 what she takes back

  the inboxes named after the three personas -> received  stored only; she reads them on her clock
  the reading inbox                          -> read_a_round  one stretch of reading

  her tools (``ctx.tool``)  each of app.living.moment.MOMENT_TOOLS, with how trimming treats what
                            it returns and whether it ends her round (the manifest's ``resident``
                            profile lists them in the order she is given them)
  segments (``ctx.segment``)  her round's prompt, and the output safety check's

  POST /admin/reading/carry-over  carry_over_reading  the old reading queue -> the reading inbox

**Her rounds take turns on one queue.** One person cannot think two things at once: her regular
rhythm and her early wakes share her queue, a turn at a time, first come first served. Each of the
two drops a tick while it already has one queued or running (``DROP_WHILE_ACTIVE``): one tick
waiting behind a stuck round is enough, since it decides on what is true when it gets its turn.
A turn holds her queue for at most :data:`HOLD_LIMIT`; a turn that never ends is cut off there
and handled as a crash. Each tick reads the database and compares; most run nothing. Her diary and
her weekly look back do not take turns on this queue.

**Her diary and her weekly look back have a queue each** ("day-page", "persona-review"), with the
same hold limit, so writing yesterday's page or rewriting who she is never keeps her from living
today, nor each other. Every tick of their wakes is queued (``QUEUE_EACH``): a tick waiting behind a
stuck one gets its turn, and decides then whether there is still something to write. Whether a tick
counts is judged by when it ticked (the window), what it writes by when it got its turn
(:func:`app.living.day_page.day_page_due`, :func:`app.living.persona_review.persona_review_due`).

**Clocks tick denser than the work they drive.** A clock's seconds are fixed when it is
registered, so the intervals that are business parameters (ten minutes between rounds, the
diary and review windows) are judged on every tick, in the wake's ``due``; most ticks run
nothing. Her early wake ticks every minute so that someone calling her waits at most a
minute.

**The landing clock is not a wake.** Matching what she said with what landed puts nothing on her
queues: it takes no turn, and its writes are compare-and-set on the version chain, so it may run
while one of her turns does (:mod:`app.living.landing`). Its tick gets only the time and runs the
reconcile of this process's lane.

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

from collections.abc import Callable
from datetime import datetime, timedelta

from app.capabilities.output_safety import GUARD_OUTPUT_SEGMENT
from app.domain.chat_dataflow import ChatResponseSegment
from app.domain.safety import Recall
from app.host import Batch, Coalesce, Context, Plugin, WakeSource
from app.living import pictures, records  # noqa: F401
from app.living.day_page import DuePage, LivingDayPage, day_page_due, write_due_page
from app.living.landing import reconcile_what_she_said
from app.living.moment import (
    LIFE_MOMENT_SEGMENT,
    MOMENT_TOOLS,
    DueMoment,
    LifeMoment,
    regular_moment_due,
    run_due_moment,
)
from app.living.nudge import nudged_moment_due
from app.living.participants import READING
from app.living.persona import LIVING_PERSONAS, PersonaVersion
from app.living.persona_review import DueReview, persona_review_due, write_due_review
from app.living.reading import read_a_round
from app.living.received import open_inboxes
from app.living.records import living_lane
from app.nodes.reading_carry_over import ReadingCarryOverRequest, carry_over_reading

# How often her wakes tick (see "Clocks tick denser than the work they drive").
REGULAR_EVERY = timedelta(seconds=60)
NUDGE_EVERY = timedelta(seconds=60)
# Five minutes: 24 tries inside the two hours of the diary's and the look back's windows; a tick
# that fails (the model timed out, she wrote nothing) is tried again by the next one.
DAY_PAGE_EVERY = timedelta(minutes=5)
PERSONA_REVIEW_EVERY = timedelta(minutes=5)

# How long a turn may hold one of her queues before it is cut off and handled as a crash
# (:mod:`app.host.turns`). Not a budget for a round: it turns a turn that never ends into one that
# fails. Calibrated on prod, not picked: on 2026-09-16, 1413 prod rounds in Langfuse took 8.5 s at
# p50, 76.4 s at p95, 121.8 s at p99 and 188.9 s at the most (a world round); 900 s is 4.8 times the
# longest. That distribution counts only rounds that finished (the stuck ones left no trace), so it
# says how slow a normal round can be, which is what the limit has to stay clear of. If the log shows
# a turn cut off that was only slow, the distribution has changed: measure it again rather than
# raising the limit.
HOLD_LIMIT = timedelta(seconds=900)

# Five minutes between two matchings of what she said with what landed. Nobody waits for it: her
# rounds do not read that record, taking a message back looks the message up by its outbound id,
# and whether she still sees a message she took back is read from the channel's own row. The two
# writes it follows land seconds to tens of seconds after she acts, so any interval well above that
# matches them as well. Not denser, because every tick rescans the ones that will never match (a
# send whose outcome is unknown, a row the channel never wrote, a take-back that never succeeded),
# and that part does not shrink with time; not sparser, so that what landed is matched before her
# next regular round (ten minutes).
LANDING_EVERY = timedelta(minutes=5)


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
    for her_tool in MOMENT_TOOLS:
        ctx.tool(her_tool.tool, trim=her_tool.trim, ends_round=her_tool.ends_round)
    ctx.segment(LIFE_MOMENT_SEGMENT)
    ctx.segment(GUARD_OUTPUT_SEGMENT)
    ctx.clock(
        "landing",
        LANDING_EVERY.total_seconds(),
        lambda ts: reconcile_what_she_said(lane=lane),
    )
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
