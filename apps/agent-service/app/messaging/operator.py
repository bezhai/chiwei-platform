"""人工参与者：运维以任意身份发消息、向某个参与者提问、定一条定时消息、查记录，用于验证。

它有两部分：

* 一个叫 ``operator`` 的收件箱（在 agent-service 的接线里开设）。发给它、定时发给它的
  消息能送到；送到之后它什么都不做——内容本来就在记录里，人去记录里看。它不接受
  提问（人没法同步回答）。
* 六条运维 HTTP（``/admin/messaging/*``，在 ``app/wiring/messaging.py`` 里接线）：发、问、
  定时、查记录，以及看和重放本泳道的死信。六条都要内网凭据：这个入口能冒充任何参与者
  往任何收件箱里塞东西，也能把死信重新投回去。

发送失败时回答里带着消息 id；带着它（请求里的 ``message_id``）重试，接收方按 id 去重。

发和定时发都可以说这一条要不要叫醒收件人（``wakes_recipient``，不说就叫醒），用来验证收件方
对不叫醒的消息怎么处理。

**请求要去的泳道和实际落在的泳道不一致时，一条都不发。** 泳道没部署这个服务时，sidecar
会把请求静默落回 prod 的 pod 上；通信机制按进程自己的部署泳道收发，落回 prod 就等于
往 prod 的收件箱里发。所以六条路由都声明了 ``requires_lane_match``：请求带来的泳道
（``x-ctx-lane``，没有就是 prod）和进程的部署泳道不一致，框架在进 handler 之前就回 409，
并说出自己在哪条泳道（:mod:`app.wiring.messaging`）。每个回答都带 ``lane``。
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import HTTPException
from pydantic import Field

from app.api.middleware import get_header_var
from app.messaging.dead_letters import peek_dead_letters, replay_dead_letters
from app.messaging.message import Message, SendFailed
from app.messaging.record import read_record
from app.messaging.sending import ask, send, send_at
from app.runtime import Data, Key, node
from app.runtime.lane_policy import current_deployment_lane

logger = logging.getLogger(__name__)

OPERATOR = "operator"

_MAX_ASK_SECONDS = 300


async def note_for_operator(message: Message) -> None:
    """``operator`` 收件箱的处理：只留一行日志，内容在记录里。"""
    logger.info(
        "messaging: operator received %s %s from %s",
        message.kind,
        message.message_id,
        message.sender,
    )


def _new_request_id() -> str:
    return uuid.uuid4().hex


class OperatorSendRequest(Data):
    request_id: Annotated[str, Key] = Field(default_factory=_new_request_id)
    sender: str
    recipient: str
    body: str
    message_id: str | None = None
    wakes_recipient: bool = True

    class Meta:
        transient = True


class OperatorAskRequest(Data):
    request_id: Annotated[str, Key] = Field(default_factory=_new_request_id)
    sender: str
    recipient: str
    body: str
    timeout_seconds: float = Field(default=60.0, gt=0, le=_MAX_ASK_SECONDS)
    message_id: str | None = None

    class Meta:
        transient = True


class OperatorSendAtRequest(Data):
    request_id: Annotated[str, Key] = Field(default_factory=_new_request_id)
    sender: str
    recipient: str
    body: str
    at: datetime
    message_id: str | None = None
    wakes_recipient: bool = True

    class Meta:
        transient = True


class OperatorRecordRequest(Data):
    request_id: Annotated[str, Key] = Field(default_factory=_new_request_id)
    message_id: str | None = None
    participant: str | None = None
    limit: int = Field(default=50, ge=1, le=500)

    class Meta:
        transient = True


class OperatorDeadLettersRequest(Data):
    request_id: Annotated[str, Key] = Field(default_factory=_new_request_id)
    limit: int = Field(default=20, ge=1, le=200)

    class Meta:
        transient = True


class OperatorReplayRequest(Data):
    request_id: Annotated[str, Key] = Field(default_factory=_new_request_id)
    limit: int = Field(default=20, ge=1, le=200)

    class Meta:
        transient = True


class OperatorSendResponse(Data):
    lane: str
    message_id: Annotated[str, Key]
    delivered: bool
    reason: str | None

    class Meta:
        transient = True


class OperatorAskResponse(Data):
    lane: str
    question_id: Annotated[str, Key]
    answered: bool
    answer: str | None
    reason: str | None

    class Meta:
        transient = True


class OperatorSendAtResponse(Data):
    lane: str
    message_id: Annotated[str, Key]
    deliver_at: str

    class Meta:
        transient = True


class OperatorRecordResponse(Data):
    lane: Annotated[str, Key]
    rows: list[dict[str, Any]]

    class Meta:
        transient = True


class OperatorDeadLettersResponse(Data):
    lane: Annotated[str, Key]
    dead_letters: list[dict[str, Any]]

    class Meta:
        transient = True


class OperatorReplayResponse(Data):
    lane: Annotated[str, Key]
    replayed: int
    refused: int
    failed: int

    class Meta:
        transient = True


def _lane() -> str:
    return current_deployment_lane() or "prod"


@contextmanager
def _as_http_errors() -> Iterator[None]:
    """把通信机制的异常翻成 HTTP：参数不合规 400，发送没完成 503（带消息 id）。"""
    try:
        yield
    except ValueError as exc:
        raise HTTPException(400, detail={"lane": _lane(), "message": str(exc)}) from exc
    except SendFailed as exc:
        raise HTTPException(
            503,
            detail={"lane": _lane(), "message": str(exc), "message_id": exc.message_id},
        ) from exc


@node
async def operator_send_node(req: OperatorSendRequest) -> OperatorSendResponse:
    with _as_http_errors():
        delivery = await send(
            sender=req.sender,
            recipient=req.recipient,
            body=req.body,
            message_id=req.message_id,
            wakes_recipient=req.wakes_recipient,
        )
    return OperatorSendResponse(
        lane=_lane(),
        message_id=delivery.message_id,
        delivered=delivery.delivered,
        reason=delivery.reason,
    )


@node
async def operator_ask_node(req: OperatorAskRequest) -> OperatorAskResponse:
    with _as_http_errors():
        answer = await ask(
            sender=req.sender,
            recipient=req.recipient,
            body=req.body,
            timeout_seconds=req.timeout_seconds,
            message_id=req.message_id,
        )
    return OperatorAskResponse(
        lane=_lane(),
        question_id=answer.question_id,
        answered=answer.answered,
        answer=answer.text,
        reason=answer.reason,
    )


@node
async def operator_send_at_node(req: OperatorSendAtRequest) -> OperatorSendAtResponse:
    with _as_http_errors():
        message_id = await send_at(
            sender=req.sender,
            recipient=req.recipient,
            body=req.body,
            at=req.at,
            message_id=req.message_id,
            wakes_recipient=req.wakes_recipient,
        )
    return OperatorSendAtResponse(
        lane=_lane(), message_id=message_id, deliver_at=req.at.isoformat()
    )


@node
async def operator_record_node(req: OperatorRecordRequest) -> OperatorRecordResponse:
    with _as_http_errors():
        rows = await read_record(
            message_id=req.message_id, participant=req.participant, limit=req.limit
        )
    return OperatorRecordResponse(lane=_lane(), rows=rows)


@node
async def operator_dead_letters_node(
    req: OperatorDeadLettersRequest,
) -> OperatorDeadLettersResponse:
    with _as_http_errors():
        rows = await peek_dead_letters(limit=req.limit)
    return OperatorDeadLettersResponse(lane=_lane(), dead_letters=rows)


@node
async def operator_replay_node(req: OperatorReplayRequest) -> OperatorReplayResponse:
    with _as_http_errors():
        result = await replay_dead_letters(
            limit=req.limit, operator=get_header_var("operator")
        )
    return OperatorReplayResponse(lane=_lane(), **result)
