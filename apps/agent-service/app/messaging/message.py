"""一条消息的形状、参与者名字的规则、三种操作交回给调用方的结果。

外层只有五样：消息 id、发送方、接收方、时间、类型。其余一切（谁、什么时候、在哪、
发生了什么）写在正文里，正文是自然语言。没有地点编号、没有房间 id——机制不知道也
不需要知道世界里的任何结构。

``time`` 是这条消息的时间。定时送达的是指定送达的那个时刻；立即发送的默认是发送那一刻，
发送方也可以给它所说的事发生的那一刻（比如补发一条早先没发出去的消息，沿用原来的时间），
接收方要按发生的先后排，看的是它，不是到达的先后。
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# 参与者的名字就是它在世界里的名字（赤尾、千凪、world、operator），不另设 id，谁都不需要
# 名字和 id 的对照表。名字要写进队列名（``inbox_<名字>_<泳道>``、``questions_<名字>_<泳道>``）
# 和 topic routing key（``inbox.<名字>.<泳道>``、``questions.<名字>.<泳道>``），所以只收 Unicode
# 字母、数字、``-``、``_``，以字母或数字开头。
# 不收的各有原因：点是 routing key 的分隔符，``*`` / ``#`` 写进绑定就成了通配符，空白和控制
# 字符（NUL 进不了 Postgres 的 text）在队列名、日志、记录里都会出问题，``:`` 留给下面
# :func:`broker_form` 的写法，``/`` 之类的标点也一并不收。``\w`` 在 Unicode 模式下就是"字母或
# 数字（``str.isalnum``）或下划线"。
_PARTICIPANT = re.compile(r"[^\W_][\w-]*")

# 名字在 broker 上的写法（:func:`broker_form`）最多这么多个字符。队列名和 routing key 都是
# AMQP 的 shortstr，最长 255 个字节；泳道是 K8s 资源名 ``<App>-<泳道>`` 的一部分，最长 63 个
# 字节。最长的 ``questions_<名字>_<泳道>`` 是 10 + 128 + 1 + 63 = 202 个字节，留有余量。ASCII 名字
# 就是 128 个字符；中文名一个字写出来大约 3 个字符，四十个字上下。
_BROKER_FORM_MAX = 128

# 调用方重试一次失败的发送时，沿用原来那条消息的 id（见 ``SendFailed.message_id``）。
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def participant(name: Any) -> str:
    """校验一个参与者名字，原样交回；不合规就抛 ``ValueError``。"""
    if (
        not isinstance(name, str)
        or not _PARTICIPANT.fullmatch(name)
        or len(broker_form(name)) > _BROKER_FORM_MAX
    ):
        raise ValueError(
            f"participant name {name!r} must be letters, digits, '-' or '_', start with a "
            f"letter or digit, and be short enough to fit a queue name (at most "
            f"{_BROKER_FORM_MAX} characters once written in ASCII, see broker_form)"
        )
    return name


def message_body(body: Any) -> str:
    """校验一段正文，原样交回；不合规就抛 ``ValueError``。

    正文是自然语言，什么字都可以有，除了记录者存不下的两样。记录是发送的一部分
    （:mod:`app.messaging.record`），正文要原样写进 Postgres 的 ``text`` 列：NUL 写不进去，单独的
    代理码位（U+D800–U+DFFF，不成对的半个 UTF-16 字符）编不成 UTF-8 也写不进去。这样的正文
    每一次发送都会在写记录那一步失败，重试多少次都一样，所以造消息时就拒掉。名字的规则
    （:func:`participant`）本来就不收这两样。
    """
    if not isinstance(body, str) or not body.strip():
        raise ValueError("message body must be non-empty natural-language text")
    if "\x00" in body:
        raise ValueError("message body must not contain NUL: Postgres text cannot store it")
    try:
        body.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "message body must not contain lone surrogates (U+D800-U+DFFF): they cannot be "
            "encoded as UTF-8, so the recorder cannot store them"
        ) from exc
    return body


def broker_form(name: str) -> str:
    """参与者名字写进队列名和 routing key 时的样子。

    AMQP 客户端（pamqp）按协议只收 ASCII 的队列名（``[a-zA-Z0-9-_.:@#,/ ]``），中文名不能原样
    写进去。所以只由 ASCII 组成的名字原样用；含别的字符的，写成 ``:`` 加它的 punycode
    （RFC 3492，标准库自带）：``赤尾`` → ``:bgtr75i``，``"bgtr75i".encode().decode("punycode")``
    解回来。名字里不会有 ``:``，两种写法撞不上；punycode 一一对应，不同的名字写法也不同。
    """
    if name.isascii():
        return name
    return ":" + name.encode("punycode").decode("ascii")


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
        body=message_body(body),
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
