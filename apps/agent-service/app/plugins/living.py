"""Living: the three sisters' life engine — for each sister a queue of rounds with two wakes on it.

  each sister's queue of rounds ("round", ``ctx.turns``), signalled by two wakes (``ctx.wake``):
  every  60 s  regular   -> regular_moment_due / run_due_moment   her regular rhythm
  every  60 s  nudge     -> nudged_moment_due / run_due_moment    wake her early for a call or a message

  (the household plugin opens the inboxes named after the three personas: stored only, she reads
  them on her clock; the speaking plugin has the landing clock and the two outbound queues; the
  reading plugin the reading inbox and the route that carries the old reading queue over)

  her round, which the manifest's ``resident`` profile places (app.living.moment):
    her tools (``ctx.tool``)    the methods of :class:`NotSplitYet`, each declared with how
                                trimming treats what it returns and whether it ends her round (the
                                profile lists them in the order she is given them)
    segments (``ctx.segment``)  her round's prompt (but who she is, which the persona plugin
                                registers, and the output safety check's, which the speaking
                                plugin does)
    the round before            life.moments      when her last round landed, and whether its
                                                  history was stored
    input sections              life.now
                                (the loose_ends plugin registers keep_in_mind and the state and
                                input sections about what is on her mind, the her_records plugin
                                the ones about what she just did and where she is, the phone plugin
                                her three phone tools and the sections about her phone, the
                                household plugin what reached her and the sends before and after,
                                the day_page plugin the last page she wrote)
    commit (one transaction)    life.moment   (and phone.glances, household.read of those plugins)
    after the commit            history   (Continue: logged, the round counts)

**What has not been split out is one plugin object** (:class:`NotSplitYet`), so that a tool which
needs another plugin's service can be handed it. Each plugin split out of it takes its tools along,
and what is left at the end is the life plugin.

**Her two wakes run her round from the assembled profile.** The plugin asks for ``resident`` in its
setup (``ctx.profile``); the host fills it in once every plugin has registered, before any IO, and
the wakes' ``run`` hands it to :func:`app.living.moment.run_due_moment`.

**Her rounds take turns on one queue.** One person cannot think two things at once: her regular
rhythm and her early wakes share her queue, a turn at a time, first come first served. Each of the
two drops a tick while it already has one queued or running (``DROP_WHILE_ACTIVE``): one tick
waiting behind a stuck round is enough, since it decides on what is true when it gets its turn.
A turn holds her queue for at most :data:`app.living.common.HOLD_LIMIT`; a turn that never ends is cut off there
and handled as a crash. Each tick reads the database and compares; most run nothing. Her diary and
her weekly look back have queues of their own (:mod:`app.living.day_page`,
:mod:`app.living.persona_review`).

**Clocks tick denser than the work they drive.** A clock's seconds are fixed when it is
registered, so the intervals that are business parameters (ten minutes between rounds, the
diary and review windows) are judged on every tick, in the wake's ``due``; most ticks run
nothing. Her early wake ticks every minute so that someone calling her waits at most a
minute.

**She has no inbound edge.** Messages on her phone she reads from ``common_message`` in her own
rounds; what world and her sisters tell her arrives in her inbox, which only stores it
(``tests/living/test_no_inbound.py``).

**Her Data classes come in with the modules below.** A Data class registers when its module is
imported, and the schema step builds tables only for registered classes; the split-out plugins
import their own.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial

from app.agent.contracts import Continue
from app.agent.round import AssembledProfile
from app.agent.tooling import Tool
from app.host import Batch, Coalesce, Context, Plugin, WakeSource
from app.living import moment
from app.living.common import HOLD_LIMIT
from app.living.moment import (
    LIFE_MOMENT_SEGMENTS,
    DueMoment,
    LifeMoment,
    land_the_moment,
    lost_the_last_round,
    now_section,
    previous_moment_at,
    regular_moment_due,
    remember_this_round,
    run_due_moment,
)
from app.living.nudge import nudged_moment_due
from app.living.services import (
    HerRecords,
    Phone,
    Received,
    WhoLivesHere,
)

# How often her wakes tick (see "Clocks tick denser than the work they drive").
REGULAR_EVERY = timedelta(seconds=60)
NUDGE_EVERY = timedelta(seconds=60)


class NotSplitYet:
    """What of her features has not been split out into a plugin of its own yet, as one plugin
    object. Transitional: it is gone once the last of them is split out.

    Her tools are its methods. Their bodies are still written in the modules they came from, as
    functions whose first parameter is this object, so a tool that needs another plugin's service
    reads it from ``self``, and setup hands the services in. Splitting a plugin out moves its tools
    and the state they keep into that plugin's own object, and hands the service it provides to the
    tools left here.
    """

    switch_to = moment.switch_to
    move_to = moment.move_to
    say = moment.say
    act = moment.act
    look_around = moment.look_around
    stop_for_now = moment.stop_for_now

    def __init__(self, *, residents: WhoLivesHere, her_records: HerRecords) -> None:
        self.residents = residents
        self.her_records = her_records


def regular(resident: AssembledProfile) -> WakeSource[DueMoment, LifeMoment]:
    """Her regular rhythm: a round when her current cell has none
    (:func:`app.living.moment.regular_moment_due`), run from ``resident`` on its lane (the
    process's, read when the host has filled the profile in)."""

    async def due(
        persona_id: str, batch: Batch, clock: Callable[[], datetime]
    ) -> DueMoment | None:
        return await regular_moment_due(
            lane=resident.lane, persona_id=persona_id, clock=clock
        )

    return WakeSource(
        "regular", Coalesce.DROP_WHILE_ACTIVE, due, partial(run_due_moment, resident)
    )


def nudge(
    resident: AssembledProfile, phone: Phone, received: Received
) -> WakeSource[DueMoment, LifeMoment]:
    """Her early wakes: a round when someone calls her on her phone (the ``phone`` service) or a
    message for her wants to wake her (the ``received`` service;
    :func:`app.living.nudge.nudged_moment_due`), run from ``resident`` on its lane."""

    async def due(
        persona_id: str, batch: Batch, clock: Callable[[], datetime]
    ) -> DueMoment | None:
        return await nudged_moment_due(
            lane=resident.lane,
            persona_id=persona_id,
            clock=clock,
            phone=phone,
            received=received,
        )

    return WakeSource(
        "nudge", Coalesce.DROP_WHILE_ACTIVE, due, partial(run_due_moment, resident)
    )


def setup(ctx: Context) -> None:
    residents = ctx.service("residents")
    resident = ctx.profile("resident")
    phone, received = ctx.service("phone"), ctx.service("received")
    regular_rounds, early_wakes = regular(resident), nudge(resident, phone, received)
    her_records = ctx.service("her_records")
    for persona_id in ctx.participants(profile="resident"):
        rounds = ctx.turns(persona_id, "round", hold_limit=HOLD_LIMIT)
        ctx.wake(rounds, regular_rounds, every=REGULAR_EVERY)
        ctx.wake(rounds, early_wakes, every=NUDGE_EVERY)
    living = NotSplitYet(residents=residents, her_records=her_records)
    _her_round(ctx, living)


def _her_round(ctx: Context, living: NotSplitYet) -> None:
    """Everything her round is made of; the manifest's ``resident`` profile places it."""
    _her_tools(ctx, living)
    for segment in LIFE_MOMENT_SEGMENTS:
        ctx.segment(segment)
    ctx.round_log(
        "life.moments",
        previous_at=previous_moment_at,
        lost_last_round=lost_the_last_round,
    )
    ctx.section("life.now", now_section)
    ctx.commit(
        "life.moment",
        partial(
            land_the_moment,
            loose_ends=ctx.service("loose_ends"),
            her_records=living.her_records,
        ),
    )
    ctx.after_commit("history", remember_this_round, on_failure=Continue())


def _her_tools(ctx: Context, living: NotSplitYet) -> None:
    """Her tools that have not been split out, each with how trimming treats what it returns. The
    order she is given them in is the ``resident`` profile's (part of the prompt cache's prefix:
    changing it changes the request).

    分不清就放"留着"那一档：留错了只是多占 token。
    """
    # 手上的事、当面说和做、看一眼周围、到此为止（:mod:`app.living.moment`）：
    #
    #   * ``look_around``  素材：world 回答的那一刻这里什么样，过了一阵早就变了，想知道就再看一眼。
    #   * 其余几只是**她自己动作的回执**（``switch_to`` / ``move_to`` / ``say`` / ``act`` /
    #     ``stop_for_now``），留着：这几条是"这件事到底做成了没有"的唯一记录，裁掉她就会照着一个
    #     不知道有没有成功的动作再来一遍。``stop_for_now`` 那句确认是她上一轮怎么收尾的唯一痕迹（它
    #     结束这一轮，后面没有她的话跟着），而且整条就几个字，换成短语省不下任何东西。它是唯一一只
    #     结束这一轮的手（``ends_round``）。
    ctx.tool(Tool(living.switch_to), trim="kept")
    ctx.tool(Tool(living.move_to), trim="kept")
    ctx.tool(Tool(living.say), trim="kept")
    ctx.tool(Tool(living.act), trim="kept")
    ctx.tool(Tool(living.look_around), trim="material")
    ctx.tool(Tool(living.stop_for_now), trim="kept", ends_round=True)


PLUGIN = Plugin(
    name="living",
    setup=setup,
    requires=(
        "residents",
        "her_records",
        "loose_ends",
        "phone",
        "received",
    ),
)
