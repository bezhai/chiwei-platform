"""world App 的核心插件：主 agent 的一轮、``world`` 收件箱、记录的人工读写接口。world 的清单
（:mod:`app.manifests.world`）只有它和三个知识来源的插件（:mod:`app.world.sources`，各自登记来源和
查询工具），所以 world 的进程不加载 life 的任何代码。

**主 agent 的一轮**（:mod:`app.world.main_agent`），清单 ``world`` 排它们的位置：

  segments (``ctx.segment``)   world_round，和一轮里调的 world_perception、world_npc，以及
                               一轮之外调的 world_answer
  tools (``ctx.tool``)         write_record、wake_me_at、report_change、let_npc_appear，以及
                               感知判断记判断的 someone_notices（都 kept）
  before the round             unfinished.resend        按原 id 补发没跑完的一轮的告知
  state section                records.listing          记录目录（只进清理标记）
  input sections               wake.woken_by、wake.planned、unfinished.happenings
  check                        wake.chosen              没定下次醒来，这一轮失败
  after the commit             history、unfinished.clear、wake.next（都 FailRound）

插件起来时向宿主要组好的 ``world``（``ctx.profile``），收件箱来源跑一轮时交给
:func:`app.world.main_agent.run_inbox_round`。

**收件箱** ``world``：送来的消息交给 :class:`~app.world.rounds.Rounds`，一轮处理收件箱里所有还没
经过一轮的消息，一轮一轮排在宿主给 world 的轮次队列上（``ctx.turns``），同一时刻只有一轮在跑，
一轮最多 :data:`app.world.main_agent.ROUND_TIMEOUT`；处理时限和占位租约放长到一次投递最多要等的
时间（:attr:`~app.world.rounds.Rounds.delivery_timeout`）。
只在拿着卷的写锁时消费（:func:`app.world.volume.writer_lock`）；开设时按私有状态补醒
（:func:`app.world.wake.wake_on_start`，也在拿到锁之后才跑）；状态里的最新唤醒那一轮失败时不限
次数重试、永不进死信（:func:`app.world.wake.retry_latest_wake_without_limit`）。提问由应答 agent
回答（:meth:`app.world.answer.Answering.answer_question`）：只读，不叫醒主 agent。

**``Rounds`` 和它的轮次队列在 setup 里建，每次起来一个新的。** 队列是进程内的，记着正在跑的那一轮
和排着的下一轮。宿主每次起来都相当于一个新的 world 进程：同一个 Python 进程里停了再起（回放里的
重启、被杀之后再起），也要拿到一条空的队列，不能去等上一次起来时没跑完的那一轮。放在模块级就做
不到这一点，因为模块只 import 一次。

**记录的人工读写接口**（:mod:`app.world.admin`）：四条路由都要内网凭据；请求要去的泳道不是这个
进程的泳道，就一步都不做；回答都带上这个进程的泳道。
"""
from __future__ import annotations

from functools import partial

from app.agent.contracts import FailRound
from app.agent.round import store_history
from app.agent.tooling import tool
from app.host import Context, Plugin
from app.world.actions import wake_me_at, write_record
from app.world.admin import (
    RecordDeleteRequest,
    RecordListRequest,
    RecordReadRequest,
    RecordWriteRequest,
    record_delete_node,
    record_listing_node,
    record_read_node,
    record_write_node,
)
from app.world.agents import world_segments
from app.world.answer import ANSWER, Answering
from app.world.main_agent import (
    ROUND_SEGMENTS,
    ROUND_TIMEOUT,
    clear_unfinished,
    left_over,
    next_wake_chosen,
    planned_wake,
    records_listing,
    resend_left_over,
    run_inbox_round,
    set_the_next_wake,
    woken_by,
)
from app.world.npc import NPC, Npcs
from app.world.perception import PERCEPTION, Perceiving, someone_notices
from app.world.rounds import Rounds
from app.world.volume import writer_lock
from app.world.wake import WORLD, retry_latest_wake_without_limit, wake_on_start

_RECORDS = "/admin/world/records"
_DOCUMENT = f"{_RECORDS}/document"

ROUTES = (
    ("GET", _RECORDS, RecordListRequest, record_listing_node),
    ("GET", _DOCUMENT, RecordReadRequest, record_read_node),
    ("PUT", _DOCUMENT, RecordWriteRequest, record_write_node),
    ("DELETE", _DOCUMENT, RecordDeleteRequest, record_delete_node),
)


def setup(ctx: Context) -> None:
    _the_round(ctx)
    profile = ctx.profile("world")
    rounds = Rounds(
        partial(run_inbox_round, profile),
        round_timeout=ROUND_TIMEOUT,
        turns=ctx.turns(WORLD),
    )
    ctx.inbox(
        WORLD,
        on_message=rounds.receive,
        on_question=Answering(ctx.agent(ANSWER), profile.profile.switches).answer_question,
        processing_timeout=rounds.delivery_timeout,
        on_open=wake_on_start,
        consume_while=writer_lock,
        retry_without_limit=retry_latest_wake_without_limit,
    )

    for method, path, request, handler in ROUTES:
        ctx.route(
            method,
            path,
            request,
            handler,
            inner_secret=True,
            lane_match=True,
            answers_with_lane=True,
        )


def _the_round(ctx: Context) -> None:
    """主 agent 的一轮由什么组成；清单 ``world`` 排它们的位置。"""
    for segment in world_segments(*ROUND_SEGMENTS, PERCEPTION, NPC, ANSWER).values():
        ctx.segment(segment)
    perceiving = Perceiving(ctx.agent(PERCEPTION))
    npcs = Npcs(ctx.agent(NPC), perceiving)
    for action in (
        write_record,
        wake_me_at,
        tool(perceiving.report_change),
        tool(npcs.let_npc_appear),
        someone_notices,
    ):
        ctx.tool(action, trim="kept")
    ctx.round_start("unfinished.resend", resend_left_over)
    ctx.section("records.listing", records_listing)
    ctx.section("wake.woken_by", woken_by)
    ctx.section("wake.planned", planned_wake)
    ctx.section("unfinished.happenings", left_over)
    ctx.check("wake.chosen", next_wake_chosen)
    ctx.after_commit("history", store_history, on_failure=FailRound())
    ctx.after_commit("unfinished.clear", clear_unfinished, on_failure=FailRound())
    ctx.after_commit("wake.next", set_the_next_wake, on_failure=FailRound())


PLUGIN = Plugin(name="world", setup=setup)
