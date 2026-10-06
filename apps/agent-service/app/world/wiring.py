"""world App 的接线：只有 world 的进程 import 它（``app.deployment.APP_WIRING``）。

world 的 agent 能查到的东西由知识来源提供（:mod:`app.world.sources`），在这里登记：加一个来源
就是在下面的登记表里加一行。

world 靠收件箱醒：开设名为 ``world`` 的收件箱，只在拿着卷的写锁时消费
（:func:`app.world.volume.writer_lock`；启动补醒也在拿到锁之后才跑）。送来的消息交给
:data:`ROUNDS`：一轮处理收件箱里所有还没经过一轮的消息，同一时刻只有一轮在跑，一轮最多
:data:`app.world.main_agent.ROUND_TIMEOUT`；收件箱的处理时限和占位租约放长到一次投递最多要等的
时间（:attr:`app.world.rounds.Rounds.delivery_timeout`）。开设时按私有状态补醒
（:func:`app.world.wake.wake_on_start`），状态里的最新唤醒那一轮失败时不限次数重试、永不
进死信（:func:`app.world.wake.retry_latest_wake_without_limit`）。问它的问题（某处现在什么样、
谁在哪）由应答 agent 回答（:func:`app.world.answer.answer_question`）：只读，不叫醒主 agent。

记录的人工读写接口（:mod:`app.world.admin`）挂在这个 App 自己的进程里，四个节点都绑在
``world`` 上：HTTP 路由只在跑它消费者的那个 App 的进程里挂得上。
"""
from app.messaging.receiving import inbox
from app.runtime import Source, bind, wire
from app.runtime.source import SourceSpec
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
from app.world.sources import reality, register, told
from app.world.sources import records as records_source
from app.world.volume import writer_lock
from app.world.wake import WORLD, retry_latest_wake_without_limit, wake_on_start

for source in (records_source.SOURCE, reality.SOURCE, told.SOURCE):
    register(source)

# 这个进程里 world 的一轮接一轮。
ROUNDS = Rounds(run_round, round_timeout=ROUND_TIMEOUT)

inbox(
    WORLD,
    on_message=ROUNDS.receive,
    on_question=answer_question,
    processing_timeout=ROUNDS.delivery_timeout,
    on_open=wake_on_start,
    retry_without_limit=retry_latest_wake_without_limit,
    consume_while=writer_lock,
)


def _operator_route(path: str, method: str) -> SourceSpec:
    return Source.http(
        path,
        method=method,
        response=True,
        requires_inner_secret=True,
        answers_with_lane=True,
        requires_lane_match=True,
    )


_RECORDS = "/admin/world/records"
_DOCUMENT = f"{_RECORDS}/document"

for data_type, method, path, consumer in (
    (RecordListRequest, "GET", _RECORDS, record_listing_node),
    (RecordReadRequest, "GET", _DOCUMENT, record_read_node),
    (RecordWriteRequest, "PUT", _DOCUMENT, record_write_node),
    (RecordDeleteRequest, "DELETE", _DOCUMENT, record_delete_node),
):
    bind(consumer).to_app(WORLD)
    wire(data_type).from_(_operator_route(path, method)).to(consumer)
