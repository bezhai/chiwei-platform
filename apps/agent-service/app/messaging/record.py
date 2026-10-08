"""记录者：保存经过通信机制的每一条消息，包括没送达的，只给人看。

只有这里写 ``message_record`` 这张表。参与者不读它，也不能靠它恢复自己的状态——
它是给 dashboard 和排查用的。读它的只有运维入口（:mod:`app.messaging.operator`）。

**记录是发送的一部分。** 每一次投递分三步，各自提交（见
:func:`app.messaging.sending.publish_recorded`）：

  1. 先写一行 ``sending`` 并提交——写不进去就不发，抛 ``SendFailed``；
  2. 发给 broker、等确认——没确认就补一行 ``unconfirmed``，抛 ``SendFailed``；
  3. 再写一行结果（``delivered`` / ``scheduled``）——写不进去同样抛 ``SendFailed``。

所以不会出现"broker 收下了、表里却没有这条消息"：broker 能收到的每一条，表里都先有
一行 ``sending``。进程在第 2、3 步之间死掉，表里停在 ``sending``，意思是"交出去了，
结果没来得及记下"。三种 ``SendFailed`` 都带着消息 id，调用方沿用同一个 id 重试，接收
方按 id 去重，不会处理两遍。

表是追加写的：同一条消息的每一次状态变化各占一行，不回头改旧行。一行里带着那条消息
的全部外层字段和正文，单看一行就能读懂。
``message_time`` 是消息自己的时间（定时送达时就是指定的时刻），``recorded_at`` 是
这一行写下的时刻——``delivered`` 那一行的 ``recorded_at`` 就是送达时间。

``lane`` 列是写入进程的部署泳道。ppe 泳道和 prod 共用一个库，靠这一列分开。

表的声明在 :class:`app.data.models.MessageRecord`，跟公共层的业务表一样建：coe-* 泳道启动
时由 :func:`app.data.bootstrap.ensure_business_schema` 建，prod 在发版之前走 DDL 申请
（``docs/runbooks/prod-ddl-backlog.md``）。App 启动时的运行时迁移
（:func:`app.runtime.migrator.migrate_schema`）不建它。
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import text

from app.data.session import get_session
from app.messaging.broker import lane_label
from app.messaging.message import Message, SendFailed


class Outcome(StrEnum):
    # 马上要交给 broker；后面没有结果行，就是交出去之后没来得及记下结果。
    SENDING = "sending"
    # broker 没有确认。可能没到，也可能到了；沿用同一个 id 重试是安全的。
    UNCONFIRMED = "unconfirmed"
    SCHEDULED = "scheduled"
    DELIVERED = "delivered"
    NOT_DELIVERED = "not_delivered"
    NO_ANSWER = "no_answer"
    # 拥有者点名不限次数重试的那条消息又处理失败了一次，已经排好下一次；reason 里是错误和
    # 下一次的延时（:mod:`app.messaging.receiving`）。
    RETRYING = "retrying"


async def record(
    message: Message,
    outcome: Outcome,
    *,
    reason: str | None = None,
    in_reply_to: str | None = None,
) -> None:
    """追加一行并提交。写不进去抛 ``SendFailed``（带消息 id）。"""
    try:
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO message_record (lane, message_id, kind, sender, "
                    "recipient, body, message_time, in_reply_to, outcome, reason) "
                    "VALUES (:lane, :mid, :kind, :sender, :recipient, :body, "
                    ":mtime, :reply, :outcome, :reason)"
                ),
                {
                    "lane": lane_label(),
                    "mid": message.message_id,
                    "kind": str(message.kind),
                    "sender": message.sender,
                    "recipient": message.recipient,
                    "body": message.body,
                    "mtime": message.time,
                    "reply": in_reply_to,
                    "outcome": str(outcome),
                    "reason": reason,
                },
            )
    except Exception as exc:
        raise SendFailed(
            f"could not record message {message.message_id} ({outcome}): {exc}",
            message_id=message.message_id,
        ) from exc


async def read_record(
    *,
    message_id: str | None = None,
    participant: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """本泳道的记录，按写入先后排。``participant`` 匹配发送方或接收方。"""
    clauses = ["lane = :lane"]
    params: dict[str, Any] = {"lane": lane_label(), "limit": max(1, min(limit, 500))}
    if message_id:
        clauses.append("(message_id = :mid OR in_reply_to = :mid)")
        params["mid"] = message_id
    if participant:
        clauses.append("(sender = :who OR recipient = :who)")
        params["who"] = participant
    sql = (
        "SELECT * FROM (SELECT message_id, kind, sender, recipient, body, "
        "message_time, in_reply_to, outcome, reason, lane, recorded_at, id "
        "FROM message_record WHERE " + " AND ".join(clauses) + " "
        "ORDER BY id DESC LIMIT :limit) latest ORDER BY id"
    )
    async with get_session() as s:
        rows = (await s.execute(text(sql), params)).mappings().all()
    return [{k: v for k, v in row.items() if k != "id"} for row in rows]
