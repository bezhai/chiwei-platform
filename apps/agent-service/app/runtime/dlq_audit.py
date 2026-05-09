"""Phase 7b Gap 12: runtime_dlq_audit DDL + helpers.

Status state machine (see spec §3.2 6-step protocol):
  cleared -> requeued | publish_failed | zombie_acked | already_succeeded
"""
from __future__ import annotations

import json
from enum import StrEnum

from sqlalchemy import text

from app.data.session import get_session


class AuditAction(StrEnum):
    REQUEUE = "requeue"
    CLEAR_IDEMPOTENT = "clear-idempotent"


class AuditStatus(StrEnum):
    CLEARED = "cleared"
    REQUEUED = "requeued"
    PUBLISH_FAILED = "publish_failed"
    ZOMBIE_ACKED = "zombie_acked"
    ALREADY_SUCCEEDED = "already_succeeded"


RUNTIME_DLQ_AUDIT_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS runtime_dlq_audit (
        id BIGSERIAL PRIMARY KEY,
        action TEXT NOT NULL,
        status TEXT NOT NULL,
        queue TEXT,
        queue_kind TEXT,
        message_ids JSONB,
        recovery_token TEXT,
        recovery_hint TEXT,
        cleared_inflight_count INT,
        requeued_count INT,
        operator TEXT,
        trace_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS runtime_dlq_audit_queue_idx
    ON runtime_dlq_audit (queue, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS runtime_dlq_audit_status_idx
    ON runtime_dlq_audit (status) WHERE status != 'requeued'
    """,
]


async def insert_audit_row(
    *, action: AuditAction, status: AuditStatus,
    queue: str | None, queue_kind: str | None,
    message_ids: list[str] | None,
    recovery_token: str | None, recovery_hint: str | None,
    cleared_inflight_count: int, requeued_count: int,
    operator: str | None, trace_id: str | None,
) -> int:
    # asyncpg requires explicit JSONB cast via cast() in the SQL; inline ::jsonb
    # in a parameterised query confuses the SQLAlchemy/asyncpg dialect.
    mids_json = json.dumps(message_ids) if message_ids else None
    async with get_session() as s:
        row = await s.execute(text(
            "INSERT INTO runtime_dlq_audit "
            "(action, status, queue, queue_kind, message_ids, "
            " recovery_token, recovery_hint, cleared_inflight_count, "
            " requeued_count, operator, trace_id) "
            "VALUES (:a, :s, :q, :qk, cast(:mids AS jsonb), :rt, :rh, :cic, :rc, "
            "        :op, :tid) RETURNING id"
        ), {
            "a": str(action), "s": str(status),
            "q": queue, "qk": queue_kind,
            "mids": mids_json,
            "rt": recovery_token, "rh": recovery_hint,
            "cic": cleared_inflight_count, "rc": requeued_count,
            "op": operator, "tid": trace_id,
        })
        await s.commit()
        return row.scalar()


async def update_audit_status(
    audit_id: int, status: AuditStatus,
    *, requeued_count: int | None = None,
    recovery_hint: str | None = None,
) -> None:
    async with get_session() as s:
        await s.execute(text(
            "UPDATE runtime_dlq_audit "
            "SET status=:s, updated_at=now(), "
            "    requeued_count=COALESCE(:rc, requeued_count), "
            "    recovery_hint=COALESCE(:rh, recovery_hint) "
            "WHERE id=:i"
        ), {"s": str(status), "rc": requeued_count, "rh": recovery_hint,
            "i": audit_id})
        await s.commit()
