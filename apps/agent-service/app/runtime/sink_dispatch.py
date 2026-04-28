"""Sink dispatch — emit() -> mq.publish() adapter.

Phase 2 把 ``Sink.mq("queue")`` 真正跑起来：emit 一个 Data 时，对每条
``wire(Data).to(Sink.mq(name))`` 通过 ``ALL_ROUTES`` 查到对应的 ``Route``
(queue + routing_key)，然后用现有 ``mq.publish(route, body)`` 发出去。
``mq.publish`` 内部会按 ``current_lane()`` 做 lane 队列 + routing key
后缀，这部分行为对 sink dispatch 透明。

校验在 ``compile_graph`` 启动期（``app/runtime/graph.py``）做了：找不到
queue 直接 raise GraphError，所以这里 ``_route_by_queue`` 返回 None
是不该发生的事——用 assert 防御就够了。
"""
from __future__ import annotations

from app.infra.rabbitmq import ALL_ROUTES, Route, mq
from app.runtime.data import Data
from app.runtime.sink import SinkSpec


async def _dispatch_mq_sink(sink: SinkSpec, data: Data) -> None:
    queue_name = sink.params["queue"]
    route = _route_by_queue(queue_name)
    assert route is not None, (
        f"compile_graph should have rejected Sink.mq({queue_name!r}) — "
        f"reaching dispatch is a runtime invariant violation"
    )
    body = data.model_dump(mode="json")
    await mq.publish(route, body)


def _route_by_queue(queue_name: str) -> Route | None:
    for r in ALL_ROUTES:
        if r.queue == queue_name:
            return r
    return None
