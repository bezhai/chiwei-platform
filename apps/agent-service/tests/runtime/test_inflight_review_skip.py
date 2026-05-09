"""Phase 7b Gap 18 round-4 finding 1: claim_inflight skips review terminal."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.inflight import (
    claim_inflight,
    mark_review,
)

pytestmark = pytest.mark.integration


async def _row_state(edge_id: str, idem_key: str) -> str:
    async with get_session() as s:
        r = await s.execute(text(
            "SELECT state FROM runtime_inflight "
            "WHERE edge_id=:e AND idempotent_key=:k"
        ), {"e": edge_id, "k": idem_key})
        return r.scalar_one()


async def test_claim_skips_succeeded(inflight_db: object) -> None:
    edge, key = "edgeA::cons", "k1"
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO runtime_inflight (edge_id, idempotent_key, "
            "data_table, state, attempts) VALUES (:e, :k, 't', 'succeeded', 1)"
        ), {"e": edge, "k": key})
        await s.commit()
    out = await claim_inflight(
        edge_id=edge, idempotent_key=key, data_table="t",
        worker_id="w", lease_ms=1000,
    )
    assert out.action == "skip"


async def test_claim_skips_review(inflight_db: object) -> None:
    edge, key = "edgeA::cons", "k2"
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO runtime_inflight (edge_id, idempotent_key, "
            "data_table, state, attempts) VALUES (:e, :k, 't', 'review', 1)"
        ), {"e": edge, "k": key})
        await s.commit()
    out = await claim_inflight(
        edge_id=edge, idempotent_key=key, data_table="t",
        worker_id="w", lease_ms=1000,
    )
    assert out.action == "skip"


async def test_mark_review_writes_review_state(inflight_db: object) -> None:
    edge, key = "edgeA::cons", "k3"
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO runtime_inflight (edge_id, idempotent_key, "
            "data_table, state, attempts) VALUES (:e, :k, 't', 'processing', 1)"
        ), {"e": edge, "k": key})
        await s.commit()
    await mark_review(edge_id=edge, idempotent_key=key,
                      last_error="needs operator approval")
    async with get_session() as s:
        row = (await s.execute(text(
            "SELECT state, last_error FROM runtime_inflight "
            "WHERE edge_id=:e AND idempotent_key=:k"
        ), {"e": edge, "k": key})).mappings().first()
    assert row["state"] == "review"
    assert row["last_error"] == "needs operator approval"
