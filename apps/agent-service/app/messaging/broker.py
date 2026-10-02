"""通信机制在 RabbitMQ 上的队列布局，以及它唯一的发布口。

布局（``<泳道>`` 是进程自己的部署泳道，prod 没有这一段后缀；``<名字>`` 是参与者名字写成
ASCII 的样子，见 :func:`app.messaging.message.broker_form`，``world`` 还是 ``world``，``赤尾``
是 ``:bgtr75i``）：

  收件箱      ``inbox_<名字>_<泳道>``          rk ``inbox.<名字>.<泳道>``
  问题        ``questions_<名字>_<泳道>``      rk ``questions.<名字>.<泳道>``
  定时送达    ``messaging_scheduled_<泳道>``   rk ``messaging.scheduled.<泳道>``
  死信        ``isolated_dead_letters_<泳道>``
  回答        每个进程一条私有队列，rk ``messaging.reply.<随机串>.<泳道>``

收件箱只装普通消息和退回的告知；问它的问题进它旁边那条问题队列，两条都由拥有者开设收件箱时
一起声明，各自消费（见 :mod:`app.messaging.receiving`）。前缀不同，所以不管名字里有没有
``_``，一个参与者的问题队列都不会跟另一个参与者的收件箱同名。

收件箱、问题、定时三类队列都是 ``Route.isolated``：没有消费者时不转回 prod、闲置不过期、
死信进本泳道自己那一条。泳道只取进程的部署环境（``LANE``），不看请求上下文：coe 泳道连的是
独立的 broker，ppe 泳道和 prod 共用 broker，靠队列名和 routing key 上的泳道后缀分开。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.infra.rabbitmq import X_DELAY_MAX_MS, Route, lane_queue, mq
from app.messaging.message import SendFailed, broker_form
from app.runtime.lane_policy import current_deployment_lane
from app.runtime.propagation import Context, inject_context, outbound_context

SCHEDULED = Route("messaging_scheduled", "messaging.scheduled", isolated=True)

# 一次延时最多能排多远。定时送达超过它的部分由定时队列的消费方分段：到了一段的
# 终点还没到时刻，就按剩下的时长再排一段（见 :mod:`app.messaging.receiving`）。
DELAY_LIMIT_MS = X_DELAY_MAX_MS


def hop_delay_ms(at: datetime, now: datetime) -> int:
    """从 ``now`` 起，下一段要延时多少毫秒才能离 ``at`` 最近又不超过上限。"""
    remaining = int((at - now).total_seconds() * 1000)
    return max(0, min(remaining, DELAY_LIMIT_MS))


def lane() -> str | None:
    """进程的部署泳道；prod 是 ``None``。"""
    return current_deployment_lane()


def lane_label() -> str:
    """进程的部署泳道写成字符串（prod 写成 ``prod``）：记录和去重状态里用它区分泳道。"""
    return lane() or "prod"


def inbox_route(name: str) -> Route:
    form = broker_form(name)
    return Route(f"inbox_{form}", f"inbox.{form}", isolated=True)


def question_route(name: str) -> Route:
    form = broker_form(name)
    return Route(f"questions_{form}", f"questions.{form}", isolated=True)


def reply_route(rk: str) -> Route:
    return Route("", rk, isolated=True)


async def opened(route: Route) -> bool:
    """这条泳道里有没有这条收件箱（或问题）队列。它们只由拥有者开设收件箱时声明，从不过期，
    所以"在"就是"开设过"。"""
    return await mq.queue_exists(lane_queue(route.queue, lane()))


def headers(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """出站消息头：当前 trace + 部署泳道，加上调用方给的那几项。"""
    return inject_context(
        extra, Context(trace_id=outbound_context().trace_id, lane=lane())
    )


async def publish(
    route: Route,
    body: dict[str, Any],
    *,
    headers: dict[str, Any],
    delay_ms: int | None = None,
) -> None:
    """发到本泳道的 ``route`` 上，等 broker 确认；没确认就抛 ``SendFailed``。"""
    confirmed = await mq.publish_with_confirm(
        route, body, headers=headers, delay_ms=delay_ms, lane=lane()
    )
    if not confirmed:
        raise SendFailed(f"broker did not confirm the publish to {route.rk}")
