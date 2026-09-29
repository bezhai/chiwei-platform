"""本泳道的死信：查看，和重放回原来的收件箱或定时队列。

处理失败、重试用完的消息由 broker 按队列参数送进 ``isolated_dead_letters_<泳道>``，
消息体和消息头原样保留，原去处写在 broker 加的 ``x-death`` 头里。

**只碰本部署泳道自己的死信，只发回本部署泳道自己的队列。** ppe 泳道和 prod 共用一个
broker，队列名不带泳道后缀的那一条就是 prod 的。所以：

* 来源队列不由调用方指定，永远是本进程部署泳道的那一条；
* 目的地从死信头里读出来之后，要能还原成本泳道里通信机制自己的一条队列（某个收件箱，
  或者定时队列），队列名和 routing key 都对得上才发；对不上的留在死信队列里不动，
  计入 ``refused``。

入口在 :mod:`app.messaging.operator`，和人工参与者同一套凭据和泳道核对。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import aiormq

from app.infra.rabbitmq import (
    ISOLATED_DEAD_LETTERS,
    Route,
    dead_letter_origin,
    lane_queue,
    mq,
)
from app.messaging.broker import SCHEDULED, inbox_route, lane, publish
from app.messaging.message import Message, SendFailed, participant
from app.runtime.dlq_audit import (
    AuditAction,
    AuditStatus,
    insert_audit_row,
    update_audit_status,
)
from app.runtime.retry import DELIVERY_COUNT_HEADER

logger = logging.getLogger(__name__)

# broker 每送一次死信就往头里加这几项。重放时去掉，重试计数清零，让它从头算起。
_DEATH_HEADERS = (
    "x-death",
    "x-first-death-queue",
    "x-first-death-reason",
    "x-first-death-exchange",
    "x-last-death-queue",
    "x-last-death-reason",
    "x-last-death-exchange",
    "x-delay",
)


def dead_letter_queue() -> str:
    return lane_queue(ISOLATED_DEAD_LETTERS, lane())


def _own_route(origin: Route | None) -> Route | None:
    """死信头里的去处，如果它是本泳道里通信机制自己的一条队列，交回那条 Route。"""
    if origin is None:
        return None
    here = lane()
    base_rk = origin.rk
    if here is not None:
        suffix = f".{here}"
        if not base_rk.endswith(suffix):
            return None
        base_rk = base_rk[: -len(suffix)]
    if base_rk == SCHEDULED.rk:
        route = SCHEDULED
    elif base_rk.startswith("inbox."):
        try:
            route = inbox_route(participant(base_rk[len("inbox.") :]))
        except ValueError:
            return None
    else:
        return None
    if origin.queue != lane_queue(route.queue, here):
        return None
    return route


async def _take(channel, limit: int) -> list[Any]:
    """从本泳道死信队列里取出至多 ``limit`` 条（未确认）。队列还不存在就是空的。"""
    try:
        queue = await channel.declare_queue(dead_letter_queue(), passive=True)
    except aiormq.exceptions.ChannelNotFoundEntity:
        return []
    taken = []
    for _ in range(limit):
        incoming = await queue.get(no_ack=False, fail=False)
        if incoming is None:
            break
        taken.append(incoming)
    return taken


def _describe(incoming) -> dict[str, Any]:
    received = dict(incoming.headers or {})
    deaths = received.get("x-death")
    latest = deaths[0] if isinstance(deaths, list) and deaths else {}
    origin = dead_letter_origin(received)
    try:
        message = json.loads(incoming.body)
    except Exception:
        message = {"unreadable": incoming.body[:200].decode("utf-8", "replace")}
    return {
        "message": message,
        "origin": origin.queue if origin else None,
        "reason": latest.get("reason") if isinstance(latest, dict) else None,
        "times_dead_lettered": latest.get("count") if isinstance(latest, dict) else None,
        "retries": received.get(DELIVERY_COUNT_HEADER),
    }


async def peek_dead_letters(*, limit: int) -> list[dict[str, Any]]:
    """看本泳道死信队列里的前 ``limit`` 条，看完原样放回。"""
    channel = await mq.open_channel(prefetch_count=max(1, limit))
    try:
        taken = await _take(channel, limit)
        rows = [_describe(m) for m in taken]
        for m in taken:
            await m.nack(requeue=True)
        return rows
    finally:
        if not channel.is_closed:
            await channel.close()


async def replay_dead_letters(*, limit: int, operator: str | None) -> dict[str, int]:
    """把本泳道死信队列里的至多 ``limit`` 条发回它们原来的队列。

    发回去的消息重试计数清零；runtime_inflight 里它停在 ``failed``，下一次处理直接接管。
    每一条都在 ``runtime_dlq_audit`` 里留一行。
    """
    replayed = refused = failed = 0
    channel = await mq.open_channel(prefetch_count=max(1, limit))
    held: list[Any] = []
    try:
        for incoming in await _take(channel, limit):
            received = dict(incoming.headers or {})
            route = _own_route(dead_letter_origin(received))
            try:
                message = Message.from_json(json.loads(incoming.body))
            except Exception:
                message = None
            audit_id = await insert_audit_row(
                action=AuditAction.REQUEUE,
                status=AuditStatus.CLEARED,
                queue=dead_letter_queue(),
                queue_kind="dlq",
                message_ids=[message.message_id] if message else None,
                recovery_token=message.message_id if message else None,
                recovery_hint=None,
                cleared_inflight_count=0,
                requeued_count=0,
                operator=operator,
                trace_id=None,
            )
            if route is None or message is None:
                refused += 1
                held.append(incoming)
                await update_audit_status(
                    audit_id,
                    AuditStatus.PUBLISH_FAILED,
                    recovery_hint=(
                        "refused: the dead letter's origin is not a messaging queue "
                        f"of lane {lane() or 'prod'}, or its body is not a message"
                    ),
                )
                continue
            headers = {k: v for k, v in received.items() if k not in _DEATH_HEADERS}
            headers[DELIVERY_COUNT_HEADER] = 0
            try:
                await publish(route, message.to_json(), headers=headers)
            except SendFailed as exc:
                failed += 1
                held.append(incoming)
                await update_audit_status(
                    audit_id, AuditStatus.PUBLISH_FAILED, recovery_hint=str(exc)
                )
                continue
            await update_audit_status(
                audit_id,
                AuditStatus.REQUEUED,
                requeued_count=1,
                recovery_hint=f"back to {lane_queue(route.queue, lane())}",
            )
            await incoming.ack()
            replayed += 1
    finally:
        for incoming in held:
            await incoming.nack(requeue=True)
        if not channel.is_closed:
            await channel.close()
    logger.info(
        "messaging: dead-letter replay in lane=%s replayed=%d refused=%d failed=%d",
        lane() or "prod",
        replayed,
        refused,
        failed,
    )
    return {"replayed": replayed, "refused": refused, "failed": failed}
