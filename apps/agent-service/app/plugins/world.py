"""world App 的插件：知识来源、``world`` 收件箱、记录的人工读写接口。world 的清单
（:data:`app.deployment.APPS`）只有这一个插件，所以 world 的进程不加载 life 的任何代码。

**知识来源**（:mod:`app.world.sources`）：登记 :data:`SOURCES`，停下时清空登记表。加一个来源就是
在 :data:`SOURCES` 里加一项。

**收件箱** ``world``：送来的消息交给 :class:`~app.world.rounds.Rounds`，一轮处理收件箱里所有还没
经过一轮的消息，同一时刻只有一轮在跑，一轮最多 :data:`app.world.main_agent.ROUND_TIMEOUT`；处理
时限和占位租约放长到一次投递最多要等的时间（:attr:`~app.world.rounds.Rounds.delivery_timeout`）。
只在拿着卷的写锁时消费（:func:`app.world.volume.writer_lock`）；开设时按私有状态补醒
（:func:`app.world.wake.wake_on_start`，也在拿到锁之后才跑）；状态里的最新唤醒那一轮失败时不限
次数重试、永不进死信（:func:`app.world.wake.retry_latest_wake_without_limit`）。提问由应答 agent
回答（:func:`app.world.answer.answer_question`）：只读，不叫醒主 agent。

**``Rounds`` 在 setup 里建，每次起来一个新的。** 它带着一把进程内的锁和排着的下一轮。宿主每次起来
都相当于一个新的 world 进程：同一个 Python 进程里停了再起（回放里的重启、被杀之后再起），也要拿到
一把没人拿着的锁，不能去等上一次起来时没跑完的那一轮。放在模块级就做不到这一点，因为模块只
import 一次。

**记录的人工读写接口**（:mod:`app.world.admin`）：四条路由都要内网凭据；请求要去的泳道不是这个
进程的泳道，就一步都不做；回答都带上这个进程的泳道。
"""
from __future__ import annotations

from app.host import Context, Plugin
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
from app.world.answer import answer_question
from app.world.main_agent import ROUND_TIMEOUT, run_round
from app.world.rounds import Rounds
from app.world.sources import clear_sources, reality, register, told
from app.world.sources import records as records_source
from app.world.volume import writer_lock
from app.world.wake import WORLD, retry_latest_wake_without_limit, wake_on_start

# world 的 agent 能查到的东西，按登记的先后排（:func:`app.world.sources.query_tools`）。
SOURCES = (records_source.SOURCE, reality.SOURCE, told.SOURCE)

_RECORDS = "/admin/world/records"
_DOCUMENT = f"{_RECORDS}/document"

ROUTES = (
    ("GET", _RECORDS, RecordListRequest, record_listing_node),
    ("GET", _DOCUMENT, RecordReadRequest, record_read_node),
    ("PUT", _DOCUMENT, RecordWriteRequest, record_write_node),
    ("DELETE", _DOCUMENT, RecordDeleteRequest, record_delete_node),
)


def setup(ctx: Context) -> None:
    # 先登记撤销再登记来源：setup 中途失败时，宿主停下也会把已经登记的来源清掉。
    ctx.on_stop(clear_sources)
    for source in SOURCES:
        register(source)

    rounds = Rounds(run_round, round_timeout=ROUND_TIMEOUT)
    ctx.inbox(
        WORLD,
        on_message=rounds.receive,
        on_question=answer_question,
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


PLUGIN = Plugin(name="world", setup=setup)
