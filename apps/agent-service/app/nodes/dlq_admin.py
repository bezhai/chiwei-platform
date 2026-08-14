"""Phase 7b Gap 12: admin DLQ replay nodes.

Each function is paired with a Source.http(...) admin route in
wiring/admin.py. The 6-step requeue protocol implementation lives in
dlq_requeue_impl below; see spec §3.2.

Runtime-internal primitives are accessed through ``DLQAdminCapability``
(plan B6 — public facade) so this module does not import from
``app.runtime.*`` private submodules. The only ``app.runtime`` import is
``node``, which is the framework's public decorator (re-exported from
``app.runtime.__init__``).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.capabilities.dlq import (
    AuditAction,
    AuditStatus,
    DLQAdminCapability,
)
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
from app.infra.rabbitmq import (
    ALL_ROUTES,
    CHANNEL_PARTITIONED_ROUTES,
    channel_route_for_payload,
    current_lane,
    mq,
)
from app.runtime import node

logger = logging.getLogger(__name__)

# Module-level capability singleton. Constructed lazily by all _impls; tests
# may swap ``_cap`` to inject mocks (see tests/runtime/test_dlq_admin.py).
_cap = DLQAdminCapability()


# ---------------------------------------------------------------------------
# inspect

async def dlq_inspect_impl(*, queue: str, limit: int = 20,
                           queue_kind: str = "dlq") -> list[dict[str, Any]]:
    raw = await _cap.peek(queue=queue, limit=limit)
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
    result = await _cap.clear_inflight(
        by=by,
        trace_id=body.get("trace_id"),
        edge_id=body.get("edge_id"),
        idempotent_key=body.get("idempotent_key"),
    )
    if result.already_succeeded:
        await _cap.open_audit(
            action=AuditAction.CLEAR_IDEMPOTENT,
            status=AuditStatus.ALREADY_SUCCEEDED,
            queue=None, queue_kind=None, message_ids=None,
            recovery_token=None,
            recovery_hint=(
                f"edge_id={result.edge_id} "
                f"idempotent_key={result.idempotent_key}"
            ),
            cleared_inflight_count=0, requeued_count=0,
            operator=operator, trace_id=body.get("trace_id"),
        )
        return {
            "status_code": 409, "error": "AlreadySucceeded",
            "edge_id": result.edge_id,
            "idempotent_key": result.idempotent_key,
        }
    audit_id = await _cap.open_audit(
        action=AuditAction.CLEAR_IDEMPOTENT,
        status=AuditStatus.CLEARED,
        queue=None, queue_kind=None, message_ids=None,
        recovery_token=None, recovery_hint=None,
        cleared_inflight_count=result.deleted,
        requeued_count=0, operator=operator, trace_id=body.get("trace_id"),
    )
    return {
        "status_code": 200,
        "deleted": result.deleted,
        "skipped_succeeded": result.skipped_succeeded,
        "audit_id": audit_id,
    }


# ---------------------------------------------------------------------------
# dry-run

async def dlq_dry_run_impl(body: dict[str, Any]) -> dict[str, Any]:
    queue = body["queue"]
    limit = body.get("limit", 20)
    raw = await _cap.peek(queue=queue, limit=limit)
    plan = []
    for m in raw:
        try:
            payload_obj = json.loads(m.get("payload", "{}"))
        except Exception:
            payload_obj = {}
        base = payload_obj.get("origin_queue") or queue.replace("-dlx", "")
        entry: dict[str, Any] = {
            "message_id": (m.get("properties") or {}).get("message_id"),
            "will_clear_idempotent": True,
            "target_queue": base,
        }
        # 预览必须走跟 requeue 同一条解析：报 base 名而实际投分区队列（或实际会被
        # 拒绝），等于在运维决定要不要重放的那一刻给错信息。
        if base in CHANNEL_PARTITIONED_ROUTES:
            try:
                entry["target_queue"] = channel_route_for_payload(
                    base, payload_obj.get("data") or payload_obj.get("payload") or {}
                ).queue
            except ValueError as exc:
                entry["target_queue"] = None
                entry["blocked_reason"] = str(exc)
        plan.append(entry)
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
        audit_id = await _cap.open_audit(
            action=AuditAction.REQUEUE, status=AuditStatus.CLEARED,
            queue=queue, queue_kind=body.get("queue_kind", "dlq"),
            message_ids=[msg_id], recovery_token=msg_id,
            recovery_hint=None, cleared_inflight_count=0,
            requeued_count=0, operator=operator,
            trace_id=envelope.get("trace_id"),
        )

        # step 3: clear idempotent (edge_idempotent precise mode)
        if clear:
            clear_result = await _cap.clear_inflight(
                by="edge_idempotent",
                edge_id=envelope.get("edge_id"),
                idempotent_key=envelope.get("idempotent_key"),
            )
            if clear_result.already_succeeded:
                await _cap.update_audit(
                    audit_id, status=AuditStatus.ZOMBIE_ACKED,
                    recovery_hint="inflight already succeeded; DLQ message acked as zombie",
                )
                await msg.ack()
                zombie_acked += 1
                continue

        # step 4: publish-with-confirm to original queue
        #
        # 出站队列按 channel 分区之后，origin_queue 记的 base 名（chat_response /
        # recall）不再是投递目标：base Route 仍在 ALL_ROUTES 里（它兼作 Sink.mq(name)
        # 的合法名字白名单），照着它发 publish 会成功，但那条队列没有任何消费者——
        # 消息静默滞留，审计还写着 requeued。所以重放跟正常出站走同一条规则：按被重放
        # payload 自己的 channel 现算真实的分区 route。
        target_queue = envelope.get("origin_queue") or queue.replace("-dlx", "")
        body_payload = envelope.get("data") or envelope.get("payload")
        if target_queue in CHANNEL_PARTITIONED_ROUTES:
            try:
                route = channel_route_for_payload(target_queue, body_payload)
            except ValueError as exc:
                # 分区之前进 DLQ 的老消息可能根本没有 channel 字段。不猜渠道：猜错是
                # 把回复从另一个渠道发出去，发 base 队列是没人消费的静默滞留，两者都
                # 比明确失败糟。消息 nack 回 DLQ，改完可以再放。
                await _cap.update_audit(
                    audit_id, status=AuditStatus.PUBLISH_FAILED,
                    recovery_hint=f"cannot resolve a partitioned route: {exc}",
                )
                await msg.nack(requeue=True)
                publish_failed += 1
                continue
        else:
            route = next((r for r in ALL_ROUTES if r.queue == target_queue), None)
            if route is None:
                await _cap.update_audit(
                    audit_id, status=AuditStatus.PUBLISH_FAILED,
                    recovery_hint=f"no Route for target_queue={target_queue!r}",
                )
                await msg.nack(requeue=True)
                publish_failed += 1
                continue
        confirmed = await mq.publish_with_confirm(
            route, body_payload,
            headers=envelope.get("headers") or {},
            lane=envelope.get("lane") or current_lane(),
        )
        if not confirmed:
            await _cap.update_audit(
                audit_id, status=AuditStatus.PUBLISH_FAILED,
                recovery_hint="publish_with_confirm returned False; "
                              "DLQ message nacked back; idempotent already cleared",
            )
            await msg.nack(requeue=True)
            publish_failed += 1
            continue

        # step 5 + 6
        await _cap.update_audit(
            audit_id, status=AuditStatus.REQUEUED, requeued_count=1,
        )
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
