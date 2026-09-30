"""Wiring: 通信机制的人工参与者（运维入口 + ``operator`` 收件箱）。

六条运维 HTTP 都要内网凭据（``requires_inner_secret``），每个回答都带执行它的进程
所在的泳道（``answers_with_lane``），请求要去的泳道不是这个进程所在的泳道就一条都不做
（``requires_lane_match``）。从开发机过来走 monitor-dashboard 的
``/dashboard/api/ops/messaging/*`` 转发：那一侧认 PAAS_TOKEN、落审计、带着凭据和
泳道头转到这里。

  POST /admin/messaging/send     以任意身份发给某个参与者
  POST /admin/messaging/ask      以任意身份向某个参与者提问，同步拿回答
  POST /admin/messaging/send-at  以任意身份定一条指定时刻送达的消息
  GET  /admin/messaging/record   查本泳道的通信记录
  GET  /admin/messaging/dead-letters         看本泳道的死信（看完原样放回）
  POST /admin/messaging/dead-letters/replay  把本泳道的死信发回本泳道原来的队列

``operator`` 收件箱在这里开设：发给人工参与者、以及它定给自己的消息都能送到。
"""
from app.messaging.operator import (
    OPERATOR,
    OperatorAskRequest,
    OperatorDeadLettersRequest,
    OperatorRecordRequest,
    OperatorReplayRequest,
    OperatorSendAtRequest,
    OperatorSendRequest,
    note_for_operator,
    operator_ask_node,
    operator_dead_letters_node,
    operator_record_node,
    operator_replay_node,
    operator_send_at_node,
    operator_send_node,
)
from app.messaging.receiving import inbox
from app.runtime import Source, wire
from app.runtime.source import SourceSpec


def _operator_route(path: str, method: str) -> SourceSpec:
    return Source.http(
        path,
        method=method,
        response=True,
        requires_inner_secret=True,
        answers_with_lane=True,
        requires_lane_match=True,
    )


wire(OperatorSendRequest).from_(_operator_route("/admin/messaging/send", "POST")).to(
    operator_send_node
)
wire(OperatorAskRequest).from_(_operator_route("/admin/messaging/ask", "POST")).to(
    operator_ask_node
)
wire(OperatorSendAtRequest).from_(
    _operator_route("/admin/messaging/send-at", "POST")
).to(operator_send_at_node)
wire(OperatorRecordRequest).from_(_operator_route("/admin/messaging/record", "GET")).to(
    operator_record_node
)

wire(OperatorDeadLettersRequest).from_(
    _operator_route("/admin/messaging/dead-letters", "GET")
).to(operator_dead_letters_node)
wire(OperatorReplayRequest).from_(
    _operator_route("/admin/messaging/dead-letters/replay", "POST")
).to(operator_replay_node)

inbox(OPERATOR, on_message=note_for_operator)
