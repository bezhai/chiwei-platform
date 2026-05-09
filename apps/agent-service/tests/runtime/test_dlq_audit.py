"""Phase 7b Gap 12: runtime_dlq_audit DDL + helpers."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.dlq_audit import (
    AuditAction,
    AuditStatus,
    insert_audit_row,
    update_audit_status,
)

pytestmark = pytest.mark.integration


async def test_insert_and_update_status_round_trip(dlq_audit_db: object) -> None:
    audit_id = await insert_audit_row(
        action=AuditAction.REQUEUE, status=AuditStatus.CLEARED,
        queue="durable_some_dlx", queue_kind="dlq",
        message_ids=["m1"], recovery_token="m1",
        recovery_hint=None, cleared_inflight_count=1,
        requeued_count=0, operator="alice", trace_id="t-x",
    )
    assert audit_id > 0
    await update_audit_status(audit_id, AuditStatus.REQUEUED, requeued_count=1)
    async with get_session() as s:
        row = (await s.execute(text(
            "SELECT status, requeued_count FROM runtime_dlq_audit WHERE id=:i"
        ), {"i": audit_id})).mappings().first()
    assert row["status"] == "requeued"
    assert row["requeued_count"] == 1
