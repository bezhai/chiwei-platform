"""life 插件：她的一轮本身 —— 三姐妹各一条轮次队列，两个唤醒把她带到那一刻；她亲身在世界里的几只手、
她一轮 prompt 里的三段、"现在"那一段，和这一轮怎么落地。

  * 三姐妹各一条轮次队列（``round``），各挂两个唤醒，钟都是一分钟一拍：``regular`` 是常规节奏
    （:func:`app.living.life.moment.regular_moment_due`），``nudge`` 是有人在手机上叫她、或者收件箱里有
    一条要叫醒她时提前叫醒她（:func:`app.living.life.nudge.nudged_moment_due`）；跑的都是
    :func:`app.living.life.moment.run_due_moment`。
  * 六只手（:class:`app.living.life.moment.InPerson` 的方法）：``switch_to``、``move_to``、``say``、
    ``act``、``look_around``、``stop_for_now``。
  * 她一轮 prompt 的三段：``this-moment``、``rules``、``voice``（「她是谁」那一段由 persona 插件登记，
    「手边能读的说明」由 guides 插件，嘴交出去之前那道检查的由 speaking 插件）。
  * 上一轮的记录 ``life.moments``（上一轮什么时候落地、它的上下文存下来没有）；输入段 ``life.now``
    （几点了、离上一次隔了多久，:mod:`app.living.life.now`）。
  * 落地那个事务里的 ``life.moment``（:class:`app.living.life.moment.LifeMoment`；同一个事务里还有 phone
    插件的 ``phone.glances``、household 插件的 ``household.read``）；落地之后的 ``history``：把这一轮写进
    她的上下文，失败只记日志，这一轮照样算数。
  * 要 ``residents`` 服务（说给谁、问 world 时署的名）、``her_records`` 服务（她在哪、做过什么）、
    ``loose_ends`` 服务（落地时她还挂着几件）、``phone`` 和 ``received`` 服务（提前叫醒她时问手机上、
    收件箱里有什么在叫她）。

**两个唤醒按组好的那份跑她的一轮。** setup 里要 ``resident``（``ctx.profile``）；宿主在每个插件都登记完、
任何 IO 之前把它填好，唤醒的 ``run`` 把它交给 :func:`app.living.life.moment.run_due_moment`。

**她的轮次在一条队列上轮流。** 一个人不能同时想两件事：常规节奏和提前叫醒共用她的队列，一次一轮，先来
先到。两个来源各自已经有一拍在排队或者正在跑时，新来的那一拍当场丢掉（``DROP_WHILE_ACTIVE``）：排在一轮
卡住的后面有一拍就够了，它轮到时按那一刻的事实判。一轮最多占着她的队列
:data:`app.living.common.HOLD_LIMIT`，一直不结束的那一轮到点掐断，按崩溃处理。每一拍都读库比一比，大多数
什么都不跑。日记和每周回看各有自己的队列（:mod:`app.living.day_page`、:mod:`app.living.persona_review`）。

**钟拍得比它驱动的事密。** 钟的秒数在登记时就定死了，所以属于业务参数的间隔（两轮之间十分钟）在每一拍
的 ``due`` 里判，大多数拍什么都不跑。提前叫醒一分钟一拍，叫她的人最多等一分钟。

**她没有入口。** 手机上的消息她在自己的轮次里从 ``common_message`` 读；world 和姐妹告诉她的进她的收件箱，
收件只存储，她醒来时读（``tests/living/test_no_inbound.py``）。

**LifeMoment 和 NudgeBegun 随这里 import 的两个模块登记。** Data 类在它的模块被 import 时登记，建表那一步
只建登记过的类。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial

from app.agent.contracts import Continue
from app.agent.round import AssembledProfile
from app.agent.tooling import Tool
from app.host import Batch, Coalesce, Context, Plugin, WakeSource
from app.living.common import HOLD_LIMIT
from app.living.life.moment import (
    LIFE_MOMENT_SEGMENTS,
    DueMoment,
    InPerson,
    LifeMoment,
    land_the_moment,
    lost_the_last_round,
    previous_moment_at,
    regular_moment_due,
    remember_this_round,
    run_due_moment,
)
from app.living.life.now import now_section
from app.living.life.nudge import nudged_moment_due
from app.living.services import Phone, Received

# 两个唤醒的钟多久拍一次（见上面"钟拍得比它驱动的事密"）。
REGULAR_EVERY = timedelta(seconds=60)
NUDGE_EVERY = timedelta(seconds=60)


def regular_wake(resident: AssembledProfile) -> WakeSource[DueMoment, LifeMoment]:
    """她的常规节奏：她现在这一格还没跑过一轮，就跑一轮（:func:`app.living.life.moment.regular_moment_due`），
    按 ``resident`` 在它的泳道上跑（进程的泳道，宿主填好这份时读的）。"""

    async def due(
        persona_id: str, batch: Batch, clock: Callable[[], datetime]
    ) -> DueMoment | None:
        return await regular_moment_due(
            lane=resident.lane, persona_id=persona_id, clock=clock
        )

    return WakeSource(
        "regular", Coalesce.DROP_WHILE_ACTIVE, due, partial(run_due_moment, resident)
    )


def nudge_wake(
    resident: AssembledProfile, phone: Phone, received: Received
) -> WakeSource[DueMoment, LifeMoment]:
    """提前叫醒她：有人在手机上叫她（``phone`` 服务），或者有一条给她的消息要叫醒她（``received`` 服务；
    :func:`app.living.life.nudge.nudged_moment_due`），就跑一轮，按 ``resident`` 在它的泳道上跑。"""

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
    resident = ctx.profile("resident")
    phone, received = ctx.service("phone"), ctx.service("received")
    regular_rounds, early_wakes = regular_wake(resident), nudge_wake(resident, phone, received)
    for persona_id in ctx.participants(profile="resident"):
        rounds = ctx.turns(persona_id, "round", hold_limit=HOLD_LIMIT)
        ctx.wake(rounds, regular_rounds, every=REGULAR_EVERY)
        ctx.wake(rounds, early_wakes, every=NUDGE_EVERY)
    in_person = InPerson(residents=ctx.service("residents"), her_records=ctx.service("her_records"))
    _her_round(ctx, in_person)


def _her_round(ctx: Context, in_person: InPerson) -> None:
    """她的一轮里由这个插件登记的那些；清单的 ``resident`` 定它们的位置。"""
    _her_tools(ctx, in_person)
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
            her_records=in_person.her_records,
        ),
    )
    ctx.after_commit("history", remember_this_round, on_failure=Continue())


def _her_tools(ctx: Context, in_person: InPerson) -> None:
    """她亲身的六只手，各带裁剪时怎么对待它的返回。她拿到它们的顺序是 ``resident`` 清单的（请求前缀缓存
    的一部分：改顺序就是改请求）。

    分不清就放"留着"那一档：留错了只是多占 token。
    """
    # 手上的事、当面说和做、看一眼周围、到此为止（:class:`app.living.life.moment.InPerson`）：
    #
    #   * ``look_around``  素材：world 回答的那一刻这里什么样，过了一阵早就变了，想知道就再看一眼。
    #   * 其余几只是**她自己动作的回执**（``switch_to`` / ``move_to`` / ``say`` / ``act`` /
    #     ``stop_for_now``），留着：这几条是"这件事到底做成了没有"的唯一记录，裁掉她就会照着一个
    #     不知道有没有成功的动作再来一遍。``stop_for_now`` 那句确认是她上一轮怎么收尾的唯一痕迹（它
    #     结束这一轮，后面没有她的话跟着），而且整条就几个字，换成短语省不下任何东西。它是唯一一只
    #     结束这一轮的手（``ends_round``）。
    ctx.tool(Tool(in_person.switch_to), trim="kept")
    ctx.tool(Tool(in_person.move_to), trim="kept")
    ctx.tool(Tool(in_person.say), trim="kept")
    ctx.tool(Tool(in_person.act), trim="kept")
    ctx.tool(Tool(in_person.look_around), trim="material")
    ctx.tool(Tool(in_person.stop_for_now), trim="kept", ends_round=True)


PLUGIN = Plugin(
    name="life",
    setup=setup,
    requires=(
        "residents",
        "her_records",
        "loose_ends",
        "phone",
        "received",
    ),
)
