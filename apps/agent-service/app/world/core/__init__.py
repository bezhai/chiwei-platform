"""插件 ``world``：world 的核心——主 agent 的一轮、``world`` 收件箱、自己定的醒来、``world_intake``
服务。别的都是功能插件（记录、现实、told、感知判断、NPC、应答，各在 :mod:`app.world` 下自己的目录里），
它们依赖核心，核心不依赖它们。world 的清单（:mod:`app.manifests.world`）只有这些插件，所以 world 的
进程不加载 life 的任何代码。

**主 agent 的一轮**（:mod:`app.world.core.main_agent`），清单 ``world`` 排它们的位置：

  segments (``ctx.segment``)   world_round 的几段（核心、醒来、一轮的顺序）
  tools (``ctx.tool``)         wake_me_at（kept）
  input sections               wake.woken_by、wake.planned
  check                        wake.chosen              没定下次醒来，这一轮失败
  after the commit             history、wake.next（都 FailRound）

报告变化、没跑完的一轮的补发和清空是感知判断插件的，让 NPC 出场是 NPC 插件的（它经感知判断插件的
``perception`` 服务告知）。

插件起来时向宿主要组好的 ``world``（``ctx.profile``），收件箱来源跑一轮时交给
:func:`app.world.core.main_agent.run_inbox_round`。

**收件箱** ``world``：送来的消息先交给登记过的收件处理（``world_intake``，这个插件提供，
:class:`~app.world.core.intake.Intakes`），再交给 :class:`~app.world.core.rounds.Rounds`，一轮处理
收件箱里所有还没经过一轮的消息，一轮一轮排在宿主给 world 的轮次队列上（``ctx.turns``），同一时刻只有一轮在跑，
一轮最多 :data:`app.world.core.main_agent.ROUND_TIMEOUT`；处理时限和占位租约放长到一次投递最多要等的
时间（:attr:`~app.world.core.rounds.Rounds.delivery_timeout`）。
只在拿着卷的写锁时消费（:func:`app.world.volume.writer_lock`）；开设时按私有状态补醒
（:func:`app.world.core.wake.wake_on_start`，也在拿到锁之后才跑）；状态里的最新唤醒那一轮失败时不限
次数重试、永不进死信（:func:`app.world.core.wake.retry_latest_wake_without_limit`）。提问交给在
``world_intake`` 上登记的应答（应答插件 :mod:`app.world.answer`；没有登记就什么都不说）：只读，不叫醒
主 agent。这个插件不依赖应答插件：功能插件依赖核心，核心不反过来依赖它们。

**``Rounds`` 和它的轮次队列在 setup 里建，每次起来一个新的。** 队列是进程内的，记着正在跑的那一轮
和排着的下一轮。宿主每次起来都相当于一个新的 world 进程：同一个 Python 进程里停了再起（回放里的
重启、被杀之后再起），也要拿到一条空的队列，不能去等上一次起来时没跑完的那一轮。放在模块级就做
不到这一点，因为模块只 import 一次。
"""
from __future__ import annotations

from functools import partial

from app.agent.contracts import FailRound
from app.agent.round import store_history
from app.host import Context, Plugin
from app.world.agents import WORLD, world_segments
from app.world.core.actions import wake_me_at
from app.world.core.intake import Intakes
from app.world.core.main_agent import (
    ROUND,
    ROUND_TIMEOUT,
    next_wake_chosen,
    planned_wake,
    run_inbox_round,
    set_the_next_wake,
    woken_by,
)
from app.world.core.rounds import Rounds
from app.world.core.wake import retry_latest_wake_without_limit, wake_on_start
from app.world.volume import writer_lock

# 主 agent 的 prompt 里由这个插件登记的那几段；别的段（写记录、现实、报告变化、NPC、没跑完的一轮）
# 由各自的插件登记。
_ROUND_SEGMENTS = tuple(f"{ROUND}/{name}" for name in ("core", "wake", "order"))


def setup(ctx: Context) -> None:
    _the_round(ctx)
    profile = ctx.profile("world")
    intakes = Intakes(profile.profile.switches)
    ctx.provide("world_intake", intakes)
    rounds = Rounds(
        partial(run_inbox_round, profile),
        round_timeout=ROUND_TIMEOUT,
        turns=ctx.turns(WORLD),
        take_in=intakes.take_in,
    )
    ctx.inbox(
        WORLD,
        on_message=rounds.receive,
        on_question=intakes.answer,
        processing_timeout=rounds.delivery_timeout,
        on_open=wake_on_start,
        consume_while=writer_lock,
        retry_without_limit=retry_latest_wake_without_limit,
    )


def _the_round(ctx: Context) -> None:
    """主 agent 的一轮由什么组成；清单 ``world`` 排它们的位置。"""
    for segment in world_segments(*_ROUND_SEGMENTS).values():
        ctx.segment(segment)
    ctx.tool(wake_me_at, trim="kept")
    ctx.section("wake.woken_by", woken_by)
    ctx.section("wake.planned", planned_wake)
    ctx.check("wake.chosen", next_wake_chosen)
    ctx.after_commit("history", store_history, on_failure=FailRound())
    ctx.after_commit("wake.next", set_the_next_wake, on_failure=FailRound())


PLUGIN = Plugin(name="world", setup=setup, provides=("world_intake",))
