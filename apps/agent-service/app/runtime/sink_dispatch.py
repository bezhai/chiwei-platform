"""Sink dispatch — emit() -> mq.publish() adapter.

Phase 2 把 ``Sink.mq("queue")`` 真正跑起来：emit 一个 Data 时，对每条
``wire(Data).to(Sink.mq(name))`` 通过 ``ALL_ROUTES`` 查到对应的 ``Route``
(queue + routing_key)，然后用现有 ``mq.publish(route, body)`` 发出去。
lane 由 ``outbound_context`` 解析一次，同时喂给 header 和
``mq.publish(lane=...)``——header 的 lane 跟队列的 lane 必须同源。

Phase 7a (Gap 11): trace_id / lane 写入 header（与 durable / debounce 一致），
同时保留 body 字段中的 ``lane``（chat-response-worker.ts 仍按 body 读）。
两者并行直到 ts 侧切到 header 后下个 PR 再删 body 字段。

校验在 ``compile_graph`` 启动期（``app/runtime/graph.py``）做了：找不到
queue 直接 raise GraphError，所以这里 ``_route_by_queue`` 返回 None
是不该发生的事——用 assert 防御就够了。
"""
from __future__ import annotations

from app.infra.rabbitmq import (
    ALL_ROUTES,
    CHANNEL_PARTITIONED_ROUTES,
    Route,
    channel_route_for_payload,
    mq,
)
from app.runtime.data import Data
from app.runtime.propagation import inject_context, outbound_context
from app.runtime.sink import SinkSpec


async def _dispatch_mq_sink(sink: SinkSpec, data: Data) -> None:
    queue_name = sink.params["queue"]
    route = _route_by_queue(queue_name)
    assert route is not None, (
        f"compile_graph should have rejected Sink.mq({queue_name!r}) — "
        f"reaching dispatch is a runtime invariant violation"
    )
    body = data.model_dump(mode="json")
    # 出站队列按 channel 分区的那几条：rk 由消息自己的 channel 决定。agent-service
    # 是唯一的生产者，rk 分对了是两个消费服务不互相抢消息的前提。
    #
    # compile_graph 已校验「wire 到 channel-partitioned sink 的 Data 必须有 channel
    # 字段」，所以这里缺字段是不变量被破坏 —— channel_route_for_payload 照样抛，不给
    # 默认值：默认值会把分流错误变成静默的错投。
    if queue_name in CHANNEL_PARTITIONED_ROUTES:
        route = channel_route_for_payload(queue_name, body)
    # Lane source priority: contextvar > body.lane field (carried by some
    # Data classes for body-level routing, e.g. ChatResponseSegment) >
    # LANE env. ``outbound_context`` owns that order; passing ctx.lane back
    # into publish keeps the header and the queue on one value — omitting it
    # would let publish re-resolve via current_lane() and, on a lane pod with
    # no contextvar, route to the lane queue while the header said prod.
    raw_body_lane = body.get("lane")
    body_lane = (
        raw_body_lane if isinstance(raw_body_lane, str) and raw_body_lane else None
    )
    ctx = outbound_context(fallback_lane=body_lane)
    headers = inject_context({"data_type": type(data).__name__}, ctx)
    await mq.publish(route, body, headers=headers, lane=ctx.lane)


def _route_by_queue(queue_name: str) -> Route | None:
    for r in ALL_ROUTES:
        if r.queue == queue_name:
            return r
    return None
