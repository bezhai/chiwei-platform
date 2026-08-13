"""Phase 7b Gap 12: admin DLQ nodes (inspect / clear-idempotent / dry-run / requeue).

Patch points target the module-level ``_cap`` (DLQAdminCapability instance,
plan B6); business node no longer imports runtime internals directly.
"""
from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# 重放的路由解析（出站队列按 channel 分区之后）
#
# origin_queue 记的是分区前的 base 名（chat_response / recall）。base Route 还留在
# ALL_ROUTES 里 —— 它兼作 Sink.mq(name) 的合法名字白名单 —— 所以照着它发能发成功，
# 但那条队列现在没有任何消费者：消息静默滞留，审计还写着 requeued。重放必须跟正常
# 出站走同一条规则：按被重放 payload 自己的 channel 现算真实的分区 route。


def _dlq_msg(payload: str, origin_queue: str) -> object:
    body = (
        '{"data":' + payload + ',"data_type":"x.Y","origin_app":"agent-service",'
        '"lane":null,"trace_id":"t1","edge_id":"e1","idempotent_key":"k1",'
        f'"origin_queue":"{origin_queue}"}}'
    )
    return type("M", (), {
        "body": body.encode(),
        "ack": AsyncMock(),
        "nack": AsyncMock(),
    })()


async def _last_audit_row() -> dict:
    async with get_session() as s:
        return dict((await s.execute(text(
            "SELECT status, recovery_hint, requeued_count FROM runtime_dlq_audit "
            "ORDER BY id DESC LIMIT 1"
        ))).mappings().first())


async def test_requeue_resolves_the_channel_queue_for_a_partitioned_base(
    dlq_admin_db: object,
) -> None:
    """chat_response 的重放要落在 payload 自己 channel 的那条队列上。"""
    from app.nodes import dlq_admin as mod
    fake_msg = _dlq_msg('{"id":"x","channel":"qq"}', "chat_response")
    with patch("app.nodes.dlq_admin._basic_get_one", new=AsyncMock(return_value=fake_msg)), \
         patch.object(mod._cap, "clear_inflight",
                      new=AsyncMock(return_value=_ok_clear())), \
         patch("app.nodes.dlq_admin.mq") as mq:
        mq.publish_with_confirm = AsyncMock(return_value=True)
        body = {"queue": "chat_response-dlx", "queue_kind": "dlq", "limit": 1,
                "clear_idempotent": True}
        resp = await mod.dlq_requeue_impl(body, operator="op-x")
    route = mq.publish_with_confirm.await_args.args[0]
    assert (route.queue, route.rk) == ("chat_response_qq", "chat.response.qq")
    assert resp["requeued"] == 1
    fake_msg.ack.assert_awaited_once()


async def test_requeue_of_recall_is_partitioned_the_same_way(
    dlq_admin_db: object,
) -> None:
    from app.nodes import dlq_admin as mod
    fake_msg = _dlq_msg('{"id":"x","channel":"lark"}', "recall")
    with patch("app.nodes.dlq_admin._basic_get_one", new=AsyncMock(return_value=fake_msg)), \
         patch.object(mod._cap, "clear_inflight",
                      new=AsyncMock(return_value=_ok_clear())), \
         patch("app.nodes.dlq_admin.mq") as mq:
        mq.publish_with_confirm = AsyncMock(return_value=True)
        body = {"queue": "recall-dlx", "queue_kind": "dlq", "limit": 1,
                "clear_idempotent": True}
        await mod.dlq_requeue_impl(body, operator="op-x")
    route = mq.publish_with_confirm.await_args.args[0]
    assert (route.queue, route.rk) == ("recall_lark", "action.recall.lark")


@pytest.mark.parametrize("payload", [
    '{"id":"x"}',                 # 分区之前进 DLQ 的老消息：根本没有 channel 字段
    '{"id":"x","channel":""}',
    '{"id":"x","channel":"wechat"}',   # 从没注册过的渠道，队列压根没声明
])
async def test_requeue_refuses_to_guess_a_channel(
    dlq_admin_db: object, payload: str,
) -> None:
    """说不出 channel 就不重放 —— fail-closed，跟出站的 sink dispatch 同一条口径。

    猜一个渠道会把回复从错误的渠道发出去；发到 base 队列则是没人消费的静默滞留，
    审计还显示 published 成功。明确失败最轻，而且消息还在 DLQ 里，可以改完再放。
    """
    from app.nodes import dlq_admin as mod
    fake_msg = _dlq_msg(payload, "chat_response")
    with patch("app.nodes.dlq_admin._basic_get_one", new=AsyncMock(return_value=fake_msg)), \
         patch.object(mod._cap, "clear_inflight",
                      new=AsyncMock(return_value=_ok_clear())), \
         patch("app.nodes.dlq_admin.mq") as mq:
        mq.publish_with_confirm = AsyncMock(return_value=True)
        body = {"queue": "chat_response-dlx", "queue_kind": "dlq", "limit": 1,
                "clear_idempotent": True}
        resp = await mod.dlq_requeue_impl(body, operator="op-x")
    mq.publish_with_confirm.assert_not_awaited()
    fake_msg.ack.assert_not_awaited()
    fake_msg.nack.assert_awaited_once()
    assert resp["requeued"] == 0
    assert resp["publish_failed"] == 1
    row = await _last_audit_row()
    assert row["status"] == "publish_failed"
    assert "channel" in (row["recovery_hint"] or "")


async def test_dry_run_does_not_mutate(dlq_admin_db: object) -> None:
    from app.nodes import dlq_admin as mod
    fake = [{"payload": '{"edge_id":"e1","idempotent_key":"k1"}'}]
    with patch.object(mod._cap, "peek", new=AsyncMock(return_value=fake)):
        body = {"queue": "q", "queue_kind": "dlq", "limit": 5}
        plan = await mod.dlq_dry_run_impl(body)
    assert "plan" in plan
    assert len(plan["plan"]) == 1


async def test_dry_run_previews_the_channel_queue_a_requeue_would_reach(
    dlq_admin_db: object,
) -> None:
    """预览必须和真实重放解析出同一个目标。

    运维就是看这份预览决定要不要重放的；报 base 名而实际投分区队列，等于在
    决策点上给错信息。
    """
    from app.nodes import dlq_admin as mod
    fake = [{"payload": json.dumps({
        "origin_queue": "chat_response",
        "data": {"id": "x", "channel": "qq"},
    })}]
    with patch.object(mod._cap, "peek", new=AsyncMock(return_value=fake)):
        plan = await mod.dlq_dry_run_impl({"queue": "chat_response-dlx", "limit": 1})
    assert plan["plan"][0]["target_queue"] == "chat_response_qq"


async def test_dry_run_flags_a_payload_a_requeue_would_refuse(
    dlq_admin_db: object,
) -> None:
    """分区前的老 payload 没有 channel，重放会拒绝——预览要照实说，不能报个 base 名。"""
    from app.nodes import dlq_admin as mod
    fake = [{"payload": json.dumps({
        "origin_queue": "chat_response",
        "data": {"id": "x"},
    })}]
    with patch.object(mod._cap, "peek", new=AsyncMock(return_value=fake)):
        plan = await mod.dlq_dry_run_impl({"queue": "chat_response-dlx", "limit": 1})
    entry = plan["plan"][0]
    assert entry["target_queue"] is None
    assert "channel" in entry["blocked_reason"]
