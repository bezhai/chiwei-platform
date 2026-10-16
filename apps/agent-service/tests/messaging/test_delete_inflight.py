"""Phase 7b Gap 12: delete_inflight per-mode semantics, and the error it refuses a succeeded row with."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from app.data.persist import fetch_value
from app.data.tx import current_session, tx
from app.messaging.claims import AlreadySucceededError, delete_inflight

pytestmark = pytest.mark.integration


async def _insert(state: str, *, edge: str = "e", key: str = "k", trace: str = "t") -> None:
    async with tx():
        s = current_session()
        await s.execute(text(
            "INSERT INTO runtime_inflight (edge_id, idempotent_key, "
            "data_table, state, attempts, trace_id) "
            "VALUES (:e, :k, 'tbl', :s, 1, :t)"
        ), {"e": edge, "k": key, "s": state, "t": trace})


async def test_edge_idempotent_deletes_failed_row(inflight_db: object) -> None:
    await _insert("failed", edge="e1", key="k1")
    out = await delete_inflight(by="edge_idempotent", edge_id="e1", idempotent_key="k1")
    assert out.deleted == 1
    assert out.skipped_succeeded == 0


async def test_both_modes_delete_a_dead_lettered_row(inflight_db: object) -> None:
    """进了死信的那一行不是"已经成功"：清掉它，下一份投递照常处理。"""
    await _insert("dead_lettered", edge="e1", key="k1", trace="trace-d")
    await _insert("dead_lettered", edge="e2", key="k2", trace="trace-d")

    by_key = await delete_inflight(by="edge_idempotent", edge_id="e1", idempotent_key="k1")
    by_trace = await delete_inflight(by="trace_id", trace_id="trace-d")

    assert (by_key.deleted, by_trace.deleted, by_trace.skipped_succeeded) == (1, 1, 0)
    assert await fetch_value("SELECT count(*) FROM runtime_inflight") == 0


async def test_edge_idempotent_raises_on_succeeded(inflight_db: object) -> None:
    await _insert("succeeded", edge="e2", key="k3")
    with pytest.raises(AlreadySucceededError) as ei:
        await delete_inflight(by="edge_idempotent", edge_id="e2", idempotent_key="k3")
    assert ei.value.edge_id == "e2"
    assert ei.value.idempotent_key == "k3"


async def test_edge_idempotent_no_row_returns_zero(inflight_db: object) -> None:
    out = await delete_inflight(by="edge_idempotent", edge_id="missing", idempotent_key="x")
    assert out.deleted == 0


async def test_trace_id_deletes_only_non_succeeded(inflight_db: object) -> None:
    await _insert("failed", edge="e1", key="k1", trace="trace-a")
    await _insert("processing", edge="e2", key="k2", trace="trace-a")
    await _insert("succeeded", edge="e3", key="k3", trace="trace-a")
    out = await delete_inflight(by="trace_id", trace_id="trace-a")
    assert out.deleted == 2
    assert out.skipped_succeeded == 1
    n = await fetch_value(
        "SELECT count(*) FROM runtime_inflight WHERE trace_id='trace-a'"
    )
    assert n == 1


async def test_trace_id_does_not_raise_on_succeeded_present(inflight_db: object) -> None:
    await _insert("succeeded", edge="e1", key="k1", trace="trace-b")
    out = await delete_inflight(by="trace_id", trace_id="trace-b")
    assert out.deleted == 0
    assert out.skipped_succeeded == 1


def test_delete_inflight_rejects_unknown_mode():
    with pytest.raises(ValueError, match="by must be one of"):
        asyncio.run(delete_inflight(by="banana"))


def test_delete_inflight_rejects_missing_args():
    with pytest.raises(ValueError):
        asyncio.run(delete_inflight(by="trace_id"))  # trace_id=None
    with pytest.raises(ValueError):
        asyncio.run(delete_inflight(by="edge_idempotent", edge_id="e"))  # idempotent_key=None


def test_already_succeeded_error_carries_inflight_keys():
    exc = AlreadySucceededError(edge_id="EdgeA::consumer", idempotent_key="abc123")
    assert exc.edge_id == "EdgeA::consumer"
    assert exc.idempotent_key == "abc123"
    assert "EdgeA::consumer" in str(exc)
    assert "abc123" in str(exc)


def test_already_succeeded_error_is_exception():
    assert issubclass(AlreadySucceededError, Exception)
