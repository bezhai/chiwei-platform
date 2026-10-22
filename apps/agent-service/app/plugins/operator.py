"""Operator: the human participant of the messaging layer, its admin routes and its inbox.

  POST /admin/messaging/send                 send to a participant as anyone
  POST /admin/messaging/ask                  ask a participant as anyone, and wait for the answer
  POST /admin/messaging/send-at              schedule a message for a given time
  GET  /admin/messaging/record               this lane's messaging record
  GET  /admin/messaging/dead-letters         this lane's dead letters (put back after reading)
  POST /admin/messaging/dead-letters/replay  send this lane's dead letters back to their queues

Every route needs the inner credential, answers with this process's lane, and does nothing when
the request is meant for another lane (``x-ctx-lane``): a lane without agent-service falls back
to prod's pod, and these routes act on the lane's own state. From a dev machine they are reached
through monitor-dashboard's ``/dashboard/api/ops/messaging/*``, which checks PAAS_TOKEN, audits,
and forwards with the credential and the lane header.

The ``operator`` inbox receives what is sent to the human participant, including the messages it
scheduled for itself.
"""
from __future__ import annotations

from app.host import Context, Plugin
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

ROUTES = (
    ("POST", "/admin/messaging/send", OperatorSendRequest, operator_send_node),
    ("POST", "/admin/messaging/ask", OperatorAskRequest, operator_ask_node),
    ("POST", "/admin/messaging/send-at", OperatorSendAtRequest, operator_send_at_node),
    ("GET", "/admin/messaging/record", OperatorRecordRequest, operator_record_node),
    ("GET", "/admin/messaging/dead-letters", OperatorDeadLettersRequest, operator_dead_letters_node),
    ("POST", "/admin/messaging/dead-letters/replay", OperatorReplayRequest, operator_replay_node),
)


def setup(ctx: Context) -> None:
    for method, path, request, handler in ROUTES:
        ctx.route(
            method,
            path,
            request,
            handler,
            inner_secret=True,
            lane_match=True,
            answers_with_lane=True,
        )
    ctx.inbox(OPERATOR, on_message=note_for_operator)


PLUGIN = Plugin(name="operator", setup=setup)
