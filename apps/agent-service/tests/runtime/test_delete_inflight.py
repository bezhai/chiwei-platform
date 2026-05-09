"""Phase 7b Gap 12: delete_inflight per-mode semantics."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.errors import AlreadySucceededError
from app.runtime.inflight import delete_inflight

pytestmark = pytest.mark.integration


async def _insert(state: str, *, edge: str = "e", key: str = "k", trace: str = "t") -> None:
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO runtime_inflight (edge_id, idempotent_key, "
            "data_table, state, attempts, trace_id) "
            "VALUES (:e, :k, 'tbl', :s, 1, :t)"
        ), {"e": edge, "k": key, "s": state, "t": trace})
        await s.commit()


async def test_edge_idempotent_deletes_failed_row(inflight_db: object) -> None:
    await _insert("failed", edge="e1", key="k1")
    out = await delete_inflight(by="edge_idempotent", edge_id="e1", idempotent_key="k1")
    assert out.deleted == 1
    assert out.skipped_succeeded == 0


async def test_edge_idempotent_deletes_review_row(inflight_db: object) -> None:
    await _insert("review", edge="e1", key="k2")
    out = await delete_inflight(by="edge_idempotent", edge_id="e1", idempotent_key="k2")
    assert out.deleted == 1


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
    await _insert("review", edge="e4", key="k4", trace="trace-a")
    out = await delete_inflight(by="trace_id", trace_id="trace-a")
    assert out.deleted == 3
    assert out.skipped_succeeded == 1
    async with get_session() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM runtime_inflight WHERE trace_id='trace-a'"
        ))).scalar()
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
