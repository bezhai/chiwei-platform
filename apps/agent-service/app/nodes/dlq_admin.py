"""Phase 7b Gap 12: admin DLQ replay nodes.

Each function is paired with a Source.http(...) admin route in
wiring/admin.py. The 6-step requeue protocol implementation lives in
dlq_requeue_impl below; see spec §3.2.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.domain.dlq_admin_events import (
    DlqClearIdempotentRequest,
    DlqClearIdempotentResponse,
    DlqDryRunRequest,
    DlqDryRunResponse,
    DlqInspectRequest,
    DlqInspectResponse,
    DlqRequeueRequest,
    DlqRequeueResponse,
)
from app.infra.rabbitmq import ALL_ROUTES, current_lane, mq
from app.runtime import node
from app.runtime.dlq_audit import (
    AuditAction,
    AuditStatus,
    insert_audit_row,
    update_audit_status,
)
from app.runtime.errors import AlreadySucceededError
from app.runtime.inflight import delete_inflight
from app.runtime.rabbitmq_management import RabbitMQManagementClient

logger = logging.getLogger(__name__)

# Lazy singleton — avoids RABBITMQ_HOST KeyError at import time in tests.
_mgmt_client_instance: RabbitMQManagementClient | None = None


def _lazy_mgmt() -> RabbitMQManagementClient:
    global _mgmt_client_instance
    if _mgmt_client_instance is None:
        _mgmt_client_instance = RabbitMQManagementClient.from_env()
    return _mgmt_client_instance


# ---------------------------------------------------------------------------
# inspect

async def dlq_inspect_impl(*, queue: str, limit: int = 20,
                           queue_kind: str = "dlq") -> list[dict[str, Any]]:
    raw = await _lazy_mgmt().peek_messages(queue=queue, limit=limit)
    out = []
    for m in raw:
        headers = (m.get("properties") or {}).get("headers") or {}
        try:
            payload_obj = json.loads(m.get("payload", "{}"))
        except Exception:
            payload_obj = {"_unparseable": True}
        out.append({
            "trace_id": headers.get("trace_id"),
            "data_type": payload_obj.get("data_type"),
            "payload": payload_obj.get("payload") or payload_obj.get("data"),
            "attempts": headers.get("x-delivery-count"),
            "first_failed_at": None,  # filled by JOIN runtime_inflight in v2
        })
    return out


# ---------------------------------------------------------------------------
# clear-idempotent

async def dlq_clear_idempotent_impl(
    body: dict[str, Any], *, operator: str | None
) -> dict[str, Any]:
    by = body.get("by")
    try:
        outcome = await delete_inflight(
            by=by,
            trace_id=body.get("trace_id"),
            edge_id=body.get("edge_id"),
            idempotent_key=body.get("idempotent_key"),
        )
    except AlreadySucceededError as e:
        await insert_audit_row(
            action=AuditAction.CLEAR_IDEMPOTENT,
            status=AuditStatus.ALREADY_SUCCEEDED,
            queue=None, queue_kind=None, message_ids=None,
            recovery_token=None,
            recovery_hint=f"edge_id={e.edge_id} idempotent_key={e.idempotent_key}",
            cleared_inflight_count=0, requeued_count=0,
            operator=operator, trace_id=body.get("trace_id"),
        )
        return {"status_code": 409, "error": "AlreadySucceeded",
                "edge_id": e.edge_id, "idempotent_key": e.idempotent_key}
    audit_id = await insert_audit_row(
        action=AuditAction.CLEAR_IDEMPOTENT,
        status=AuditStatus.CLEARED,
        queue=None, queue_kind=None, message_ids=None,
        recovery_token=None, recovery_hint=None,
        cleared_inflight_count=outcome.deleted,
        requeued_count=0, operator=operator, trace_id=body.get("trace_id"),
    )
    return {
        "status_code": 200,
        "deleted": outcome.deleted,
        "skipped_succeeded": outcome.skipped_succeeded,
        "audit_id": audit_id,
    }


# ---------------------------------------------------------------------------
# dry-run

async def dlq_dry_run_impl(body: dict[str, Any]) -> dict[str, Any]:
    queue = body["queue"]
    limit = body.get("limit", 20)
    raw = await _lazy_mgmt().peek_messages(queue=queue, limit=limit)
    plan = []
    for m in raw:
        try:
            payload_obj = json.loads(m.get("payload", "{}"))
        except Exception:
            payload_obj = {}
        plan.append({
            "message_id": (m.get("properties") or {}).get("message_id"),
            "will_clear_idempotent": True,
            "target_queue": payload_obj.get("origin_queue") or queue.replace("-dlx", ""),
        })
    return {"plan": plan}


# ---------------------------------------------------------------------------
# requeue (6-step transaction-like)

async def _basic_get_one(queue: str):
    """Wrap aio_pika queue.get(no_ack=False) for DLQ replay."""
    from app.infra.rabbitmq import basic_get
    return await basic_get(queue, no_ack=False)


async def dlq_requeue_impl(body: dict[str, Any], *, operator: str | None) -> dict[str, Any]:
    queue = body["queue"]
    limit = body.get("limit", 1)
    clear = body.get("clear_idempotent", False)

    requeued = 0
    publish_failed = 0
    zombie_acked = 0

    for _ in range(limit):
        msg = await _basic_get_one(queue)
        if msg is None:
            break  # queue empty

        try:
            envelope = json.loads(msg.body)
        except Exception:
            await msg.nack(requeue=True)
            continue

        msg_id = envelope.get("message_id") or str(envelope.get("trace_id") or "")
        # step 2: audit cleared row first
        audit_id = await insert_audit_row(
            action=AuditAction.REQUEUE, status=AuditStatus.CLEARED,
            queue=queue, queue_kind=body.get("queue_kind", "dlq"),
            message_ids=[msg_id], recovery_token=msg_id,
            recovery_hint=None, cleared_inflight_count=0,
            requeued_count=0, operator=operator,
            trace_id=envelope.get("trace_id"),
        )

        # step 3: clear idempotent (edge_idempotent precise mode)
        if clear:
            try:
                await delete_inflight(
                    by="edge_idempotent",
                    edge_id=envelope.get("edge_id"),
                    idempotent_key=envelope.get("idempotent_key"),
                )
            except AlreadySucceededError:
                await update_audit_status(
                    audit_id, AuditStatus.ZOMBIE_ACKED,
                    recovery_hint="inflight already succeeded; DLQ message acked as zombie",
                )
                await msg.ack()
                zombie_acked += 1
                continue

        # step 4: publish-with-confirm to original queue
        target_queue = envelope.get("origin_queue") or queue.replace("-dlx", "")
        route = next((r for r in ALL_ROUTES if r.queue == target_queue), None)
        if route is None:
            await update_audit_status(
                audit_id, AuditStatus.PUBLISH_FAILED,
                recovery_hint=f"no Route for target_queue={target_queue!r}",
            )
            await msg.nack(requeue=True)
            publish_failed += 1
            continue
        body_payload = envelope.get("data") or envelope.get("payload")
        confirmed = await mq.publish_with_confirm(
            route, body_payload,
            headers=envelope.get("headers") or {},
            lane=envelope.get("lane") or current_lane(),
        )
        if not confirmed:
            await update_audit_status(
                audit_id, AuditStatus.PUBLISH_FAILED,
                recovery_hint="publish_with_confirm returned False; "
                              "DLQ message nacked back; idempotent already cleared",
            )
            await msg.nack(requeue=True)
            publish_failed += 1
            continue

        # step 5 + 6
        await update_audit_status(audit_id, AuditStatus.REQUEUED, requeued_count=1)
        await msg.ack()
        requeued += 1

    return {
        "status_code": 200,
        "requeued": requeued,
        "publish_failed": publish_failed,
        "zombie_acked": zombie_acked,
    }


# ---------------------------------------------------------------------------
# @node wrappers — wired to Source.http routes in wiring/admin.py

@node
async def dlq_inspect_node(req: DlqInspectRequest) -> DlqInspectResponse:
    rows = await dlq_inspect_impl(
        queue=req.queue, limit=req.limit, queue_kind=req.queue_kind,
    )
    return DlqInspectResponse(request_id=req.request_id, rows=rows)


@node
async def dlq_clear_idempotent_node(
    req: DlqClearIdempotentRequest,
) -> DlqClearIdempotentResponse:
    from app.api.middleware import operator_var
    body = {
        "by": req.by,
        "trace_id": req.trace_id,
        "edge_id": req.edge_id,
        "idempotent_key": req.idempotent_key,
    }
    resp = await dlq_clear_idempotent_impl(body, operator=operator_var.get())
    return DlqClearIdempotentResponse(
        request_id=req.request_id,
        deleted=resp.get("deleted", 0),
        skipped_succeeded=resp.get("skipped_succeeded", 0),
        error=resp.get("error"),
        edge_id=resp.get("edge_id"),
        idempotent_key=resp.get("idempotent_key"),
        status_code=resp.get("status_code", 200),
    )


@node
async def dlq_dry_run_node(req: DlqDryRunRequest) -> DlqDryRunResponse:
    body = {
        "queue": req.queue, "limit": req.limit, "queue_kind": req.queue_kind,
    }
    resp = await dlq_dry_run_impl(body)
    return DlqDryRunResponse(request_id=req.request_id, plan=resp["plan"])


@node
async def dlq_requeue_node(req: DlqRequeueRequest) -> DlqRequeueResponse:
    from app.api.middleware import operator_var
    body = {
        "queue": req.queue, "queue_kind": req.queue_kind,
        "limit": req.limit, "clear_idempotent": req.clear_idempotent,
    }
    resp = await dlq_requeue_impl(body, operator=operator_var.get())
    return DlqRequeueResponse(
        request_id=req.request_id,
        requeued=resp.get("requeued", 0),
        publish_failed=resp.get("publish_failed", 0),
        zombie_acked=resp.get("zombie_acked", 0),
        status_code=resp.get("status_code", 200),
    )
