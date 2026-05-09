"""Phase 7b Gap 12: admin DLQ nodes (inspect / clear-idempotent / dry-run / requeue)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.errors import AlreadySucceededError

pytestmark = pytest.mark.integration


async def test_inspect_returns_peeked_rows(dlq_admin_db: object) -> None:
    from app.nodes.dlq_admin import dlq_inspect_impl
    fake = [{
        "properties": {"headers": {"trace_id": "t1"}},
        "payload": '{"data_type":"x.Y","payload":{}}',
    }]
    with patch("app.nodes.dlq_admin._lazy_mgmt") as m:
        m.return_value = AsyncMock()
        m.return_value.peek_messages = AsyncMock(return_value=fake)
        rows = await dlq_inspect_impl(queue="durable_x_y_dlx", limit=5,
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
    """If delete_inflight raises AlreadySucceededError, the requeue path
    must ack the DLQ message and write a 'zombie_acked' audit row."""
    from app.nodes.dlq_admin import dlq_requeue_impl
    fake_msg = type("M", (), {
        "body": b'{"data":{"id":"x"},"data_type":"x.Y","origin_app":"agent-service","lane":null,"trace_id":"t1","edge_id":"e1","idempotent_key":"k1"}',
        "ack": AsyncMock(),
        "nack": AsyncMock(),
    })()
    with patch("app.nodes.dlq_admin._basic_get_one", new=AsyncMock(return_value=fake_msg)), \
         patch("app.nodes.dlq_admin.delete_inflight",
               new=AsyncMock(side_effect=AlreadySucceededError(edge_id="e1", idempotent_key="k1"))), \
         patch("app.nodes.dlq_admin.mq") as mq:
        mq.publish_with_confirm = AsyncMock(return_value=True)
        body = {"queue": "q", "queue_kind": "dlq", "limit": 1, "clear_idempotent": True}
        resp = await dlq_requeue_impl(body, operator="op-x")
    fake_msg.ack.assert_awaited_once()
    mq.publish_with_confirm.assert_not_awaited()
    assert resp["zombie_acked"] == 1


async def test_requeue_publish_failed_nacks_and_audits(dlq_admin_db: object) -> None:
    from app.nodes.dlq_admin import dlq_requeue_impl
    fake_msg = type("M", (), {
        "body": b'{"data":{"id":"x"},"data_type":"x.Y","origin_app":"agent-service","lane":null,"trace_id":"t1","edge_id":"e1","idempotent_key":"k1","origin_queue":"q"}',
        "ack": AsyncMock(),
        "nack": AsyncMock(),
    })()
    with patch("app.nodes.dlq_admin._basic_get_one", new=AsyncMock(return_value=fake_msg)), \
         patch("app.nodes.dlq_admin.delete_inflight", new=AsyncMock()), \
         patch("app.nodes.dlq_admin.mq") as mq:
        mq.publish_with_confirm = AsyncMock(return_value=False)
        body = {"queue": "q", "queue_kind": "dlq", "limit": 1, "clear_idempotent": True}
        resp = await dlq_requeue_impl(body, operator="op-x")
    fake_msg.nack.assert_awaited_once()
    fake_msg.ack.assert_not_awaited()
    assert resp["publish_failed"] == 1


async def test_dry_run_does_not_mutate(dlq_admin_db: object) -> None:
    from app.nodes.dlq_admin import dlq_dry_run_impl
    with patch("app.nodes.dlq_admin._lazy_mgmt") as m:
        m.return_value = AsyncMock()
        m.return_value.peek_messages = AsyncMock(return_value=[
            {"payload": '{"edge_id":"e1","idempotent_key":"k1"}'}
        ])
        body = {"queue": "q", "queue_kind": "dlq", "limit": 5}
        plan = await dlq_dry_run_impl(body)
    assert "plan" in plan
    assert len(plan["plan"]) == 1
