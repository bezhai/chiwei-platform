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

  her round, which the manifest's ``resident`` profile places (app.living.moment):
    her tools (``ctx.tool``)    the methods of :class:`NotSplitYet`, each declared with how
                                trimming treats what it returns and whether it ends her round (the
                                profile lists them in the order she is given them)
    segments (``ctx.segment``)  her round's prompt (but who she is, which the persona plugin
                                registers), and the output safety check's
    before the round            outgoing.before   send what she did and has not sent yet
    the round before            life.moments      when her last round landed, and whether its
                                                  history was stored
    state sections              day_page.last_written, phone.unread
    input sections              life.now, household.received, phone.arrived
                                (the loose_ends plugin registers keep_in_mind and the state and
                                input sections about what is on her mind, the her_records plugin
                                the ones about what she just did and where she is)
    commit (one transaction)    life.moment, phone.glances, household.read
    after the commit            history, outgoing.after   (both Continue: logged, the round counts)

  POST /admin/reading/carry-over  carry_over_reading  the old reading queue -> the reading inbox

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

**The reading inbox** (:data:`app.living.services.READING`) gets a message when she picks a
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
at import (:func:`app.living.received.open_inboxes` reads and checks them through the ``residents``
service, and declares the inboxes through the function the host hands it). Whose queues are opened
is the manifest's: the participants of the ``resident`` profile.

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
from app.domain.chat_dataflow import ChatResponseSegment
from app.domain.safety import Recall
from app.host import Batch, Coalesce, Context, Plugin, WakeSource
from app.living import (
    moment,
    mouth,
    phone,
    reading,
    takeback,
)
from app.living.common import living_lane
from app.living.day_page import DuePage, LivingDayPage, day_page_due, write_due_page
from app.living.landing import reconcile_what_she_said
from app.living.moment import (
    LIFE_MOMENT_SEGMENTS,
    DueMoment,
    LifeMoment,
    commit_her_glances,
    day_page_section,
    land_the_moment,
    lost_the_last_round,
    mark_received_read,
    now_section,
    phone_arrived_section,
    phone_unread_section,
    previous_moment_at,
    received_section,
    regular_moment_due,
    remember_this_round,
    run_due_moment,
    send_her_doings,
)
from app.living.nudge import nudged_moment_due
from app.living.output_safety import GUARD_OUTPUT_SEGMENT
from app.living.persona_review import DueReview, persona_review_due, write_due_review
from app.living.reading import read_a_round
from app.living.received import open_inboxes
from app.living.services import (
    READING,
    HerRecords,
    Persona,
    PersonaText,
    Pictures,
    WhoLivesHere,
)
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
    look_at_phone = phone.look_at_phone
    look_through_your_phone = phone.look_through_your_phone
    look_up_contact = phone.look_up_contact
    send_message = mouth.send_message
    take_back_message = takeback.take_back_message
    look_for_something_to_read = reading.look_for_something_to_read
    read_a_bit = reading.read_a_bit

    def __init__(
        self, *, residents: WhoLivesHere, her_records: HerRecords, pictures: Pictures
    ) -> None:
        self.residents = residents
        self.her_records = her_records
        self.pictures = pictures


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


def nudge(resident: AssembledProfile) -> WakeSource[DueMoment, LifeMoment]:
    """Her early wakes: a round when someone calls her on her phone or a message for her wants to
    wake her (:func:`app.living.nudge.nudged_moment_due`), run from ``resident`` on its lane."""

    async def due(
        persona_id: str, batch: Batch, clock: Callable[[], datetime]
    ) -> DueMoment | None:
        return await nudged_moment_due(
            lane=resident.lane, persona_id=persona_id, clock=clock
        )

    return WakeSource(
        "nudge", Coalesce.DROP_WHILE_ACTIVE, due, partial(run_due_moment, resident)
    )


def day_page(
    lane: str, persona: Persona, her_records: HerRecords
) -> WakeSource[DuePage, LivingDayPage | None]:
    """Her diary on ``lane``: the page of the living day that just ended, when the tick came in the
    window and the day has no page yet (:func:`app.living.day_page.day_page_due`); who she is
    comes from the ``persona`` service, what she did that day from the ``her_records`` service."""

    async def due(persona_id: str, batch: Batch, clock: Callable[[], datetime]) -> DuePage | None:
        (tick,) = batch.close()
        return await day_page_due(
            lane=lane,
            persona_id=persona_id,
            at=tick.at,
            clock=clock,
            her_records=her_records,
        )

    return WakeSource(
        "day-page", Coalesce.QUEUE_EACH, due, partial(write_due_page, persona=persona)
    )


def persona_review(lane: str, persona: Persona) -> WakeSource[DueReview, PersonaText | None]:
    """Her weekly look back on ``lane``: a new "who I am" from last week's pages, when the tick
    came in the window and this week has no review yet
    (:func:`app.living.persona_review.persona_review_due`), written to the ``persona`` service's
    chain."""

    async def due(persona_id: str, batch: Batch, clock: Callable[[], datetime]) -> DueReview | None:
        (tick,) = batch.close()
        return await persona_review_due(
            lane=lane, persona_id=persona_id, at=tick.at, clock=clock, persona=persona
        )

    return WakeSource(
        "persona-review", Coalesce.QUEUE_EACH, due, partial(write_due_review, persona=persona)
    )


def setup(ctx: Context) -> None:
    lane = living_lane()
    persona = ctx.service("persona")
    residents = ctx.service("residents")
    resident = ctx.profile("resident")
    regular_rounds, early_wakes = regular(resident), nudge(resident)
    her_records = ctx.service("her_records")
    pages, reviews = day_page(lane, persona, her_records), persona_review(lane, persona)
    for persona_id in ctx.participants(profile="resident"):
        rounds = ctx.turns(persona_id, "round", hold_limit=HOLD_LIMIT)
        ctx.wake(rounds, regular_rounds, every=REGULAR_EVERY)
        ctx.wake(rounds, early_wakes, every=NUDGE_EVERY)
        diary = ctx.turns(persona_id, "day-page", hold_limit=HOLD_LIMIT)
        ctx.wake(diary, pages, every=DAY_PAGE_EVERY)
        look_back = ctx.turns(persona_id, "persona-review", hold_limit=HOLD_LIMIT)
        ctx.wake(look_back, reviews, every=PERSONA_REVIEW_EVERY)
    living = NotSplitYet(
        residents=residents, her_records=her_records, pictures=ctx.service("pictures")
    )
    _her_round(ctx, living)
    ctx.clock(
        "landing",
        LANDING_EVERY.total_seconds(),
        lambda ts: reconcile_what_she_said(lane=lane),
    )
    ctx.inbox(
        READING,
        on_message=partial(read_a_round, persona=persona),
        processing_timeout=timedelta(minutes=5),
        max_attempts=1,
    )
    ctx.route(
        "POST",
        "/admin/reading/carry-over",
        ReadingCarryOverRequest,
        partial(carry_over_reading, residents=residents),
        inner_secret=True,
        lane_match=True,
        answers_with_lane=True,
    )
    ctx.outbound(ChatResponseSegment, "chat_response")
    ctx.outbound(Recall, "recall")
    ctx.inboxes_at_start(partial(open_inboxes, residents=residents))


def _her_round(ctx: Context, living: NotSplitYet) -> None:
    """Everything her round is made of; the manifest's ``resident`` profile places it."""
    _her_tools(ctx, living)
    send_doings = partial(
        send_her_doings, residents=living.residents, her_records=living.her_records
    )
    phone_unread = partial(phone_unread_section, her_records=living.her_records)
    phone_arrived = partial(phone_arrived_section, her_records=living.her_records)
    for segment in LIFE_MOMENT_SEGMENTS:
        ctx.segment(segment)
    ctx.segment(GUARD_OUTPUT_SEGMENT)
    ctx.round_start("outgoing.before", send_doings)
    ctx.round_log(
        "life.moments",
        previous_at=previous_moment_at,
        lost_last_round=lost_the_last_round,
    )
    for name, section in (
        ("day_page.last_written", day_page_section),
        ("phone.unread", phone_unread),
        ("life.now", now_section),
        ("household.received", received_section),
        ("phone.arrived", phone_arrived),
    ):
        ctx.section(name, section)
    ctx.commit(
        "life.moment",
        partial(
            land_the_moment,
            loose_ends=ctx.service("loose_ends"),
            her_records=living.her_records,
        ),
    )
    ctx.commit("phone.glances", commit_her_glances)
    ctx.commit("household.read", mark_received_read)
    ctx.after_commit("history", remember_this_round, on_failure=Continue())
    ctx.after_commit("outgoing.after", send_doings, on_failure=Continue())


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
    # 手机的三只手都留着，不当素材裁（:mod:`app.living.phone`）：
    #
    #   * ``look_up_contact`` / ``look_through_your_phone``  ``channel_id=<id>``，被 ``look_at_phone`` /
    #     ``send_message`` 吃（翻页那只手的最后一串还是往下翻的游标）。安静下来的会话不在手机通知上，
    #     这两只手是找回它的仅有的两条路。
    #   * ``look_at_phone``  **唯一一只返回是别人内容、却留着的手。** 它那一页上有两样凭据：每条她自己
    #     发的消息带的 ``take_back_id``，和头上那串 ``before=``。前者在快照的"你刚做过、说过"那段有
    #     副本，但那段只有最近 12 条，滚出去的旧消息就没有第二份了；后者是往前翻**唯一**的入口，从头
    #     到尾只出现在这一次返回里，换掉正文她就再也翻不回这条会话更早的地方。只保留句柄、把正文
    #     换掉更精确，但那要在裁剪这一层解析手机这边渲染出来的标签 —— 渲染改一个字，句柄就静默地留
    #     不住了，而症状是几小时后她拿着一个不存在的编号去撤消息。别人的正文因此多留 3 小时：一页十几
    #     条文本，跟这个风险不是一个量级（图片块**不**跟着多留，它走
    #     ``HistoryTexts.picture_trimmed``（:mod:`app.manifests.history`） 那条独立的线）。
    ctx.tool(Tool(living.look_at_phone), trim="kept")
    ctx.tool(Tool(living.look_through_your_phone), trim="kept")
    ctx.tool(Tool(living.look_up_contact), trim="kept")
    # 发消息这只手的回执是"这件事到底做成了没有"的唯一记录（发出去了、已经说过了、交出去但没等到
    # 确认），裁掉她就会照着一个不知道有没有成功的动作再来一遍，所以留着（:mod:`app.living.mouth`）。
    ctx.tool(Tool(living.send_message), trim="kept")
    # 撤回这只手的回执是她自己动作的结果，跟她自己的话一样留着（:mod:`app.living.takeback`）。
    ctx.tool(Tool(living.take_back_message), trim="kept")
    # 读东西的两只手留着，不当素材裁：``look_for_something_to_read`` 的返回里是 ``file=<attachment_id>``，
    # 被 ``read_a_bit(which=...)`` 吃；``read_a_bit`` 指代不明时抛的那句话里也逐个印着候选的
    # ``file=``（:mod:`app.living.reading`）。
    ctx.tool(Tool(living.look_for_something_to_read), trim="kept")
    ctx.tool(Tool(living.read_a_bit), trim="kept")


PLUGIN = Plugin(
    name="living",
    setup=setup,
    requires=("persona", "residents", "her_records", "pictures", "loose_ends"),
)
