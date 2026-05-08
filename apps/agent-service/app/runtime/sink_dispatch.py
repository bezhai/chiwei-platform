"""Sink dispatch — emit() -> mq.publish() adapter.

Phase 2 把 ``Sink.mq("queue")`` 真正跑起来：emit 一个 Data 时，对每条
``wire(Data).to(Sink.mq(name))`` 通过 ``ALL_ROUTES`` 查到对应的 ``Route``
(queue + routing_key)，然后用现有 ``mq.publish(route, body)`` 发出去。
``mq.publish`` 内部会按 ``current_lane()`` 做 lane 队列 + routing key
后缀，这部分行为对 sink dispatch 透明。

Phase 7a (Gap 11): trace_id / lane 写入 header（与 durable / debounce 一致），
同时保留 body 字段中的 ``lane``（chat-response-worker.ts 仍按 body 读）。
两者并行直到 ts 侧切到 header 后下个 PR 再删 body 字段。

校验在 ``compile_graph`` 启动期（``app/runtime/graph.py``）做了：找不到
queue 直接 raise GraphError，所以这里 ``_route_by_queue`` 返回 None
是不该发生的事——用 assert 防御就够了。
"""
from __future__ import annotations

from app.api.middleware import lane_var, trace_id_var
from app.infra.rabbitmq import ALL_ROUTES, Route, mq
from app.runtime.data import Data
from app.runtime.propagation import Context, inject_context
from app.runtime.sink import SinkSpec


async def _dispatch_mq_sink(sink: SinkSpec, data: Data) -> None:
    queue_name = sink.params["queue"]
    route = _route_by_queue(queue_name)
    assert route is not None, (
        f"compile_graph should have rejected Sink.mq({queue_name!r}) — "
        f"reaching dispatch is a runtime invariant violation"
    )
    body = data.model_dump(mode="json")
    # Lane source priority: contextvar > body.lane field (carried by some
    # Data classes for body-level routing, e.g. ChatResponseSegment).
    raw_body_lane = body.get("lane")
    body_lane = (
        raw_body_lane if isinstance(raw_body_lane, str) and raw_body_lane else None
    )
    ctx_lane = lane_var.get() or body_lane
    headers = inject_context(
        {"data_type": type(data).__name__},
        Context(trace_id=trace_id_var.get(), lane=ctx_lane),
    )
    if ctx_lane:
        await mq.publish(route, body, headers=headers, lane=ctx_lane)
    else:
        # Fall through to mq.publish's default lane resolution
        # (current_lane() -> lane_var or LANE env), preserving prior behavior.
        await mq.publish(route, body, headers=headers)


def _route_by_queue(queue_name: str) -> Route | None:
    for r in ALL_ROUTES:
        if r.queue == queue_name:
            return r
    return None
