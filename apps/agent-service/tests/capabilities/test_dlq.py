"""DLQAdminCapability — B6 facade over runtime-internal DLQ primitives.

The capability hides four runtime-internal modules from business nodes:
- ``runtime/rabbitmq_management.RabbitMQManagementClient`` (HTTP peek)
- ``runtime/inflight.delete_inflight`` + ``AlreadySucceededError``
- ``runtime/dlq_audit.{insert_audit_row, update_audit_status, AuditAction, AuditStatus}``

Each test pins one capability method to a runtime-internal collaborator so
the public surface stays a single import (``from app.capabilities.dlq
import DLQAdminCapability``) rather than four scattered runtime imports.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.capabilities._errors import CapabilityCallFailed, CapabilityInvalidArg
from app.capabilities.dlq import (
    AuditAction,
    AuditStatus,
    ClearInflightResult,
    DLQAdminCapability,
)


# ---------------------------------------------------------------------------
# peek — wraps RabbitMQManagementClient.peek_messages
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_peek_returns_raw_messages():
    cap = DLQAdminCapability()
    fake = [
        {"properties": {"headers": {"trace_id": "t1"}},
         "payload": '{"data_type":"x.Y","payload":{}}'},
    ]
    fake_mgmt = AsyncMock()
    fake_mgmt.peek_messages = AsyncMock(return_value=fake)
    with patch.object(cap, "_mgmt", lambda: fake_mgmt):
        rows = await cap.peek(queue="durable_x_y_dlx", limit=5)
    assert rows == fake
    fake_mgmt.peek_messages.assert_awaited_once_with(
        queue="durable_x_y_dlx", limit=5
    )


@pytest.mark.asyncio
async def test_peek_maps_http_error_to_capability_call_failed():
    cap = DLQAdminCapability()
    fake_mgmt = AsyncMock()
    err = httpx.HTTPStatusError(
        "500 Internal", request=httpx.Request("POST", "http://x"),
        response=httpx.Response(500),
    )
    fake_mgmt.peek_messages = AsyncMock(side_effect=err)
    with patch.object(cap, "_mgmt", lambda: fake_mgmt):
        with pytest.raises(CapabilityCallFailed) as excinfo:
            await cap.peek(queue="q", limit=1)
    assert "q" in excinfo.value.meta.get("queue", "") or "q" == excinfo.value.meta.get("queue")


# ---------------------------------------------------------------------------
# clear_inflight — wraps delete_inflight + maps AlreadySucceededError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clear_inflight_edge_success_returns_result():
    cap = DLQAdminCapability()
    fake_outcome = type("O", (), {"deleted": 1, "skipped_succeeded": 0})()
    with patch(
        "app.capabilities.dlq.delete_inflight",
        new=AsyncMock(return_value=fake_outcome),
    ) as m:
        result = await cap.clear_inflight(
            by="edge_idempotent", edge_id="e1", idempotent_key="k1",
        )
    assert isinstance(result, ClearInflightResult)
    assert result.deleted == 1
    assert result.skipped_succeeded == 0
    assert result.already_succeeded is False
    m.assert_awaited_once_with(
        by="edge_idempotent", trace_id=None,
        edge_id="e1", idempotent_key="k1",
    )


@pytest.mark.asyncio
async def test_clear_inflight_already_succeeded_returns_marker():
    """Capability MUST translate runtime's AlreadySucceededError into
    a typed-result flag so business nodes never import the runtime
    exception class directly.
    """
    from app.runtime.errors import AlreadySucceededError
    cap = DLQAdminCapability()
    with patch(
        "app.capabilities.dlq.delete_inflight",
        new=AsyncMock(side_effect=AlreadySucceededError(
            edge_id="e1", idempotent_key="k1",
        )),
    ):
        result = await cap.clear_inflight(
            by="edge_idempotent", edge_id="e1", idempotent_key="k1",
        )
    assert result.already_succeeded is True
    assert result.edge_id == "e1"
    assert result.idempotent_key == "k1"
    assert result.deleted == 0


@pytest.mark.asyncio
async def test_clear_inflight_invalid_by_raises_invalid_arg():
    cap = DLQAdminCapability()
    with pytest.raises(CapabilityInvalidArg):
        await cap.clear_inflight(by="bogus", trace_id="t1")


@pytest.mark.asyncio
async def test_clear_inflight_trace_mode_passes_through():
    cap = DLQAdminCapability()
    fake_outcome = type("O", (), {"deleted": 3, "skipped_succeeded": 2})()
    with patch(
        "app.capabilities.dlq.delete_inflight",
        new=AsyncMock(return_value=fake_outcome),
    ):
        result = await cap.clear_inflight(by="trace_id", trace_id="trA")
    assert result.deleted == 3
    assert result.skipped_succeeded == 2
    assert result.already_succeeded is False


# ---------------------------------------------------------------------------
# audit — wraps insert_audit_row / update_audit_status with enum aliases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_audit_inserts_row_returns_id():
    cap = DLQAdminCapability()
    with patch(
        "app.capabilities.dlq.insert_audit_row",
        new=AsyncMock(return_value=42),
    ) as m:
        audit_id = await cap.open_audit(
            action=AuditAction.REQUEUE,
            status=AuditStatus.CLEARED,
            queue="q", queue_kind="dlq",
            message_ids=["m1"], recovery_token="m1",
            recovery_hint=None, cleared_inflight_count=0,
            requeued_count=0, operator="op-x", trace_id="t1",
        )
    assert audit_id == 42
    # capability passes its own enum aliases through — internal layer sees
    # the *runtime* enum values because the aliases are the same StrEnum
    kwargs = m.await_args.kwargs
    assert str(kwargs["action"]) == "requeue"
    assert str(kwargs["status"]) == "cleared"


@pytest.mark.asyncio
async def test_update_audit_status_passes_through():
    cap = DLQAdminCapability()
    with patch(
        "app.capabilities.dlq.update_audit_status",
        new=AsyncMock(return_value=None),
    ) as m:
        await cap.update_audit(
            42, status=AuditStatus.REQUEUED,
            requeued_count=1, recovery_hint=None,
        )
    m.assert_awaited_once()
    args, kwargs = m.await_args
    # update_audit_status(audit_id, status, *, requeued_count, recovery_hint)
    assert args[0] == 42
    assert str(args[1]) == "requeued"
    assert kwargs["requeued_count"] == 1


# ---------------------------------------------------------------------------
# Public API: aliases stable
# ---------------------------------------------------------------------------

def test_capability_exposes_enum_aliases():
    """AuditAction / AuditStatus reachable via capability namespace so
    dlq_admin.py doesn't need to import runtime/dlq_audit directly.
    """
    assert AuditAction.REQUEUE == "requeue"
    assert AuditAction.CLEAR_IDEMPOTENT == "clear-idempotent"
    assert AuditStatus.CLEARED == "cleared"
    assert AuditStatus.REQUEUED == "requeued"
    assert AuditStatus.PUBLISH_FAILED == "publish_failed"
    assert AuditStatus.ZOMBIE_ACKED == "zombie_acked"
    assert AuditStatus.ALREADY_SUCCEEDED == "already_succeeded"
