"""DLQAdminCapability — public facade for DLQ admin operations (plan B6).

Business code that needs to inspect / requeue / clear DLQ entries imports
this single class instead of poking the four runtime-internal modules
(``rabbitmq_management``, ``inflight``, ``dlq_audit``, ``errors``). The
6-step requeue protocol orchestrated by ``app/nodes/dlq_admin.py`` stays
in business-land (it composes AMQP ``basic_get`` + Route lookup with
audit + idempotent state, which is wiring rather than a primitive); this
capability supplies the primitives.

Mapping (capability method → runtime internal):
    peek                   → RabbitMQManagementClient.peek_messages
    clear_inflight         → delete_inflight  (AlreadySucceeded → flag)
    open_audit             → insert_audit_row
    update_audit           → update_audit_status
    AuditAction / Status   → re-exported enums

Failure mapping (contract §4.8):
    upstream HTTP error      → CapabilityCallFailed
    invalid `by` arg         → CapabilityInvalidArg
    inflight already done    → ClearInflightResult(already_succeeded=True)
                               (no exception — the requeue protocol relies
                               on this to ack the zombie DLQ message)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app.capabilities._errors import CapabilityCallFailed, CapabilityInvalidArg
from app.runtime.dlq_audit import (
    AuditAction,
    AuditStatus,
    insert_audit_row,
    update_audit_status,
)
from app.runtime.errors import AlreadySucceededError
from app.runtime.inflight import delete_inflight
from app.runtime.rabbitmq_management import RabbitMQManagementClient


@dataclass(frozen=True)
class ClearInflightResult:
    """Outcome of DLQAdminCapability.clear_inflight.

    ``already_succeeded`` is True when the targeted edge_idempotent row was
    in 'succeeded' state; the runtime refuses to delete it and the requeue
    protocol must ack the DLQ message as a zombie instead of retrying.
    """
    deleted: int
    skipped_succeeded: int
    already_succeeded: bool = False
    edge_id: str | None = None
    idempotent_key: str | None = None


class DLQAdminCapability:
    """Public facade over runtime-internal DLQ admin primitives.

    Construct once per process (or per request); the underlying
    ``RabbitMQManagementClient`` is lazy-initialised from env so unit
    tests don't need RABBITMQ_* env at import time.
    """

    def __init__(
        self,
        *,
        mgmt_client_factory: Any | None = None,
    ) -> None:
        # Factory indirection lets tests inject a mock without monkey-
        # patching the runtime module. Default = RabbitMQManagementClient
        # .from_env at first use.
        self._mgmt_factory = mgmt_client_factory
        self._mgmt_instance: RabbitMQManagementClient | None = None

    def _mgmt(self) -> RabbitMQManagementClient:
        if self._mgmt_instance is None:
            if self._mgmt_factory is not None:
                self._mgmt_instance = self._mgmt_factory()
            else:
                self._mgmt_instance = RabbitMQManagementClient.from_env()
        return self._mgmt_instance

    # ---- peek -------------------------------------------------------------

    async def peek(
        self, *, queue: str, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List up to ``limit`` messages without consuming (mgmt API).

        Returns the raw RabbitMQ management response shape so business
        code can read ``properties.headers`` / ``payload`` / ``properties
        .message_id`` directly — the management API surface is part of
        the contract a DLQ admin needs.
        """
        try:
            return await self._mgmt().peek_messages(queue=queue, limit=limit)
        except (httpx.HTTPError, httpx.HTTPStatusError) as e:
            raise CapabilityCallFailed(
                f"DLQ peek failed: {e}",
                meta={"queue": queue, "limit": limit, "err": type(e).__name__},
            ) from e

    # ---- clear_inflight ---------------------------------------------------

    async def clear_inflight(
        self,
        *,
        by: Literal["edge_idempotent", "trace_id"],
        trace_id: str | None = None,
        edge_id: str | None = None,
        idempotent_key: str | None = None,
    ) -> ClearInflightResult:
        """Clear inflight rows for DLQ replay.

        Returns ClearInflightResult. If the targeted edge_idempotent row
        was already 'succeeded', the result carries ``already_succeeded=
        True`` (no exception) so the caller can ack the DLQ zombie.
        """
        if by not in ("edge_idempotent", "trace_id"):
            raise CapabilityInvalidArg(
                f"by must be 'edge_idempotent' or 'trace_id', got {by!r}",
                meta={"by": by},
            )
        try:
            outcome = await delete_inflight(
                by=by,
                trace_id=trace_id,
                edge_id=edge_id,
                idempotent_key=idempotent_key,
            )
        except AlreadySucceededError as e:
            return ClearInflightResult(
                deleted=0,
                skipped_succeeded=0,
                already_succeeded=True,
                edge_id=e.edge_id,
                idempotent_key=e.idempotent_key,
            )
        except ValueError as e:
            # delete_inflight raises ValueError for missing fields per mode
            raise CapabilityInvalidArg(str(e), meta={"by": by}) from e
        return ClearInflightResult(
            deleted=outcome.deleted,
            skipped_succeeded=outcome.skipped_succeeded,
            already_succeeded=False,
        )

    # ---- audit ------------------------------------------------------------

    async def open_audit(
        self,
        *,
        action: AuditAction,
        status: AuditStatus,
        queue: str | None,
        queue_kind: str | None,
        message_ids: list[str] | None,
        recovery_token: str | None,
        recovery_hint: str | None,
        cleared_inflight_count: int,
        requeued_count: int,
        operator: str | None,
        trace_id: str | None,
    ) -> int:
        return await insert_audit_row(
            action=action,
            status=status,
            queue=queue,
            queue_kind=queue_kind,
            message_ids=message_ids,
            recovery_token=recovery_token,
            recovery_hint=recovery_hint,
            cleared_inflight_count=cleared_inflight_count,
            requeued_count=requeued_count,
            operator=operator,
            trace_id=trace_id,
        )

    async def update_audit(
        self,
        audit_id: int,
        *,
        status: AuditStatus,
        requeued_count: int | None = None,
        recovery_hint: str | None = None,
    ) -> None:
        await update_audit_status(
            audit_id,
            status,
            requeued_count=requeued_count,
            recovery_hint=recovery_hint,
        )


__all__ = [
    "AuditAction",
    "AuditStatus",
    "ClearInflightResult",
    "DLQAdminCapability",
]
