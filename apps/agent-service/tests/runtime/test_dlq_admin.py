"""Phase 7b Gap 12: admin DLQ nodes (inspect / clear-idempotent / dry-run / requeue).

Patch points target the module-level ``_cap`` (DLQAdminCapability instance,
plan B6); business node no longer imports runtime internals directly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.capabilities.dlq import ClearInflightResult
from app.data.session import get_session

pytestmark = pytest.mark.integration


def _ok_clear(deleted: int = 1, skipped: int = 0) -> ClearInflightResult:
    return ClearInflightResult(
        deleted=deleted, skipped_succeeded=skipped, already_succeeded=False,
    )


def _zombie_clear(edge_id: str = "e1",
                  idempotent_key: str = "k1") -> ClearInflightResult:
    return ClearInflightResult(
        deleted=0, skipped_succeeded=0, already_succeeded=True,
        edge_id=edge_id, idempotent_key=idempotent_key,
    )


async def test_inspect_returns_peeked_rows(dlq_admin_db: object) -> None:
    from app.nodes import dlq_admin as mod
    fake = [{
        "properties": {"headers": {"trace_id": "t1"}},
        "payload": '{"data_type":"x.Y","payload":{}}',
    }]
    with patch.object(mod._cap, "peek", new=AsyncMock(return_value=fake)):
        rows = await mod.dlq_inspect_impl(queue="durable_x_y_dlx", limit=5,
                                          queue_kind="dlq")
    assert len(rows) == 1
    assert rows[0]["trace_id"] == "t1"


async def test_clear_idempotent_edge_succeeded_returns_409(dlq_admin_db: object) -> None:
    from app.nodes.dlq_admin import dlq_clear_idempotent_impl
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO runtime_inflight (edge_id, idempotent_key, "
            "data_table, state, attempts) "
            "VALUES ('e1', 'k1', 't', 'succeeded', 1)"
        ))
        await s.commit()
    body = {"by": "edge_idempotent", "edge_id": "e1", "idempotent_key": "k1"}
    resp = await dlq_clear_idempotent_impl(body, operator="op-x")
    assert resp["status_code"] == 409
    assert "AlreadySucceeded" in resp["error"]


async def test_clear_idempotent_trace_skips_succeeded(dlq_admin_db: object) -> None:
    from app.nodes.dlq_admin import dlq_clear_idempotent_impl
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO runtime_inflight (edge_id, idempotent_key, "
            "data_table, state, attempts, trace_id) "
            "VALUES ('e1', 'k1', 't', 'succeeded', 1, 'trA'),"
            "       ('e2', 'k2', 't', 'failed', 1, 'trA')"
        ))
        await s.commit()
    body = {"by": "trace_id", "trace_id": "trA"}
    resp = await dlq_clear_idempotent_impl(body, operator="op-x")
    assert resp["deleted"] == 1
    assert resp["skipped_succeeded"] == 1


async def test_requeue_zombie_path_acks_without_publish(dlq_admin_db: object) -> None:
    """If capability.clear_inflight returns already_succeeded, requeue
    must ack the DLQ message and write a 'zombie_acked' audit row."""
    from app.nodes import dlq_admin as mod
    fake_msg = type("M", (), {
        "body": b'{"data":{"id":"x"},"data_type":"x.Y","origin_app":"agent-service","lane":null,"trace_id":"t1","edge_id":"e1","idempotent_key":"k1"}',
        "ack": AsyncMock(),
        "nack": AsyncMock(),
    })()
    with patch("app.nodes.dlq_admin._basic_get_one", new=AsyncMock(return_value=fake_msg)), \
         patch.object(mod._cap, "clear_inflight",
                      new=AsyncMock(return_value=_zombie_clear())), \
         patch("app.nodes.dlq_admin.mq") as mq:
        mq.publish_with_confirm = AsyncMock(return_value=True)
        body = {"queue": "q", "queue_kind": "dlq", "limit": 1, "clear_idempotent": True}
        resp = await mod.dlq_requeue_impl(body, operator="op-x")
    fake_msg.ack.assert_awaited_once()
    mq.publish_with_confirm.assert_not_awaited()
    assert resp["zombie_acked"] == 1


async def test_requeue_publish_failed_nacks_and_audits(dlq_admin_db: object) -> None:
    """publish_with_confirm returns False -> nack + audit publish_failed.

    A real Route for the target queue is patched into ALL_ROUTES so the
    code path actually reaches publish_with_confirm; otherwise the impl
    short-circuits on route=None and the publish_with_confirm mock is
    never called.
    """
    from app.infra.rabbitmq import Route
    from app.nodes import dlq_admin as mod
    fake_msg = type("M", (), {
        "body": b'{"data":{"id":"x"},"data_type":"x.Y","origin_app":"agent-service","lane":null,"trace_id":"t1","edge_id":"e1","idempotent_key":"k1","origin_queue":"target_q"}',
        "ack": AsyncMock(),
        "nack": AsyncMock(),
    })()
    fake_route = Route(queue="target_q", rk="target.q")
    with patch("app.nodes.dlq_admin._basic_get_one", new=AsyncMock(return_value=fake_msg)), \
         patch.object(mod._cap, "clear_inflight",
                      new=AsyncMock(return_value=_ok_clear())), \
         patch("app.nodes.dlq_admin.ALL_ROUTES", new=[fake_route]), \
         patch("app.nodes.dlq_admin.mq") as mq:
        mq.publish_with_confirm = AsyncMock(return_value=False)
        body = {"queue": "q", "queue_kind": "dlq", "limit": 1, "clear_idempotent": True}
        resp = await mod.dlq_requeue_impl(body, operator="op-x")
    mq.publish_with_confirm.assert_awaited_once()
    fake_msg.nack.assert_awaited_once()
    fake_msg.ack.assert_not_awaited()
    assert resp["publish_failed"] == 1


async def test_requeue_success_path_publishes_and_acks(dlq_admin_db: object) -> None:
    """Happy path: publish confirms -> audit requeued + ack."""
    from app.infra.rabbitmq import Route
    from app.nodes import dlq_admin as mod
    fake_msg = type("M", (), {
        "body": b'{"data":{"id":"x"},"data_type":"x.Y","origin_app":"agent-service","lane":null,"trace_id":"t1","edge_id":"e1","idempotent_key":"k1","origin_queue":"target_q"}',
        "ack": AsyncMock(),
        "nack": AsyncMock(),
    })()
    fake_route = Route(queue="target_q", rk="target.q")
    with patch("app.nodes.dlq_admin._basic_get_one", new=AsyncMock(return_value=fake_msg)), \
         patch.object(mod._cap, "clear_inflight",
                      new=AsyncMock(return_value=_ok_clear())), \
         patch("app.nodes.dlq_admin.ALL_ROUTES", new=[fake_route]), \
         patch("app.nodes.dlq_admin.mq") as mq:
        mq.publish_with_confirm = AsyncMock(return_value=True)
        body = {"queue": "q", "queue_kind": "dlq", "limit": 1, "clear_idempotent": True}
        resp = await mod.dlq_requeue_impl(body, operator="op-x")
    mq.publish_with_confirm.assert_awaited_once()
    fake_msg.ack.assert_awaited_once()
    fake_msg.nack.assert_not_awaited()
    assert resp["requeued"] == 1
    assert resp["publish_failed"] == 0
    assert resp["zombie_acked"] == 0


async def test_dry_run_does_not_mutate(dlq_admin_db: object) -> None:
    from app.nodes import dlq_admin as mod
    fake = [{"payload": '{"edge_id":"e1","idempotent_key":"k1"}'}]
    with patch.object(mod._cap, "peek", new=AsyncMock(return_value=fake)):
        body = {"queue": "q", "queue_kind": "dlq", "limit": 5}
        plan = await mod.dlq_dry_run_impl(body)
    assert "plan" in plan
    assert len(plan["plan"]) == 1
