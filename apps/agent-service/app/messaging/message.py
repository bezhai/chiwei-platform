"""一条消息的形状、参与者名字的规则、三种操作交回给调用方的结果。

外层只有五样：消息 id、发送方、接收方、时间、类型。其余一切（谁、什么时候、在哪、
发生了什么）写在正文里，正文是自然语言。没有地点编号、没有房间 id——机制不知道也
不需要知道世界里的任何结构。

``time`` 是这条消息应当送达接收方的时刻：立即发送的就是发送那一刻，定时送达的
是指定的那个时刻。
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# 名字直接拼进队列名（``inbox_<名字>_<泳道>``）和 topic routing key（``inbox.<名字>.<泳道>``）。
# 点是 routing key 的分隔符，``*`` / ``#`` 是通配符，所以只收小写字母、数字、``-``、``_``，
# 以字母开头。
_PARTICIPANT = re.compile(r"[a-z][a-z0-9_-]{0,62}")

# 调用方重试一次失败的发送时，沿用原来那条消息的 id（见 ``SendFailed.message_id``）。
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def participant(name: Any) -> str:
    """校验一个参与者名字，原样交回；不合规就抛 ``ValueError``。"""
    if not isinstance(name, str) or not _PARTICIPANT.fullmatch(name):
        raise ValueError(
            f"participant name {name!r} must match [a-z][a-z0-9_-]{{0,62}}: "
            f"it becomes part of a queue name and a routing key"
        )
    return name


class Kind(StrEnum):
    MESSAGE = "message"
    QUESTION = "question"
    # 对一个问题的回答。只出现在提问方的回复队列和记录里，不进任何收件箱。
    ANSWER = "answer"
    # 定时消息到点时对方没有收件箱：机制发给原发送方的告知，发送方和接收方都是原发送方
    # （它自己的消息被退回）。
    NOT_DELIVERED = "not_delivered"


@dataclass(frozen=True)
class Message:
    message_id: str
    sender: str
    recipient: str
    time: datetime
    kind: Kind
    body: str

    def to_json(self) -> dict[str, str]:
        return {
            "message_id": self.message_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "time": self.time.isoformat(),
            "kind": str(self.kind),
            "body": self.body,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Message:
        return cls(
            message_id=str(data["message_id"]),
            sender=participant(data["sender"]),
            recipient=participant(data["recipient"]),
            time=datetime.fromisoformat(data["time"]),
            kind=Kind(data["kind"]),
            body=str(data["body"]),
        )


def new_message(
    *,
    sender: str,
    recipient: str,
    body: Any,
    kind: Kind,
    time: datetime | None = None,
    message_id: str | None = None,
) -> Message:
    """造一条新消息。``message_id`` 只在重试一次失败的发送时给：沿用原来的 id。"""
    if not isinstance(body, str) or not body.strip():
        raise ValueError("message body must be non-empty natural-language text")
    if message_id is not None and not (
        isinstance(message_id, str) and _MESSAGE_ID.fullmatch(message_id)
    ):
        raise ValueError(f"message id {message_id!r} must match [A-Za-z0-9_-]{{1,64}}")
    return Message(
        message_id=message_id or uuid.uuid4().hex,
        sender=participant(sender),
        recipient=participant(recipient),
        time=time or datetime.now(UTC),
        kind=kind,
        body=body,
    )


@dataclass(frozen=True)
class Delivery:
    """``send`` 的结果。``delivered=False`` 时 ``reason`` 说明为什么没送到。"""

    message_id: str
    delivered: bool
    reason: str | None = None


@dataclass(frozen=True)
class Answer:
    """``ask`` 的结果。``text is None`` 就是"没有回答"，``reason`` 说明为什么。"""

    question_id: str
    text: str | None
    reason: str | None = None

    @property
    def answered(self) -> bool:
        return self.text is not None


class SendFailed(RuntimeError):
    """这次发送没有完成：记录没写成，或者 broker 没有确认。

    消息可能没发出去，也可能已经到了对方收件箱（broker 收下之后记录没写成）。重试时
    带上 ``message_id`` 沿用同一个 id：接收方按 id 去重，不会处理两遍。
    """

    def __init__(self, reason: str, *, message_id: str | None = None) -> None:
        super().__init__(reason)
        self.message_id = message_id
