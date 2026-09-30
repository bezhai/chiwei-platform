"""world App 的接线：只有 world 的进程 import 它（``app.deployment.APP_WIRING``）。

world 靠收件箱醒：开设名为 ``world`` 的收件箱，一次只处理一条、一轮最多
:data:`app.world.main_agent.ROUND_TIMEOUT`（占位租约随之放长），开设时按私有状态补醒
（:func:`app.world.wake.wake_on_start`），自定唤醒那一轮最终进了死信就退避之后再醒
（:func:`app.world.wake.wake_after_failure`）。它不接受提问——回答"某处现在什么样"的应答
agent 还没有。

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
from app.world.main_agent import ROUND_TIMEOUT, on_world_message
from app.world.wake import WORLD, wake_after_failure, wake_on_start

inbox(
    WORLD,
    on_message=on_world_message,
    processing_timeout=ROUND_TIMEOUT,
    one_at_a_time=True,
    on_open=wake_on_start,
    on_final_failure=wake_after_failure,
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
