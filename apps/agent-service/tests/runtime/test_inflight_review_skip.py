"""Phase 7b Gap 18 round-4 finding 1: claim_inflight skips review terminal."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.data.persist import fetch_value
from app.data.tx import current_session, tx
from app.runtime.inflight import claim_inflight

pytestmark = pytest.mark.integration


async def _row_state(edge_id: str, idem_key: str) -> str:
    state = await fetch_value(
        "SELECT state FROM runtime_inflight "
        "WHERE edge_id=:e AND idempotent_key=:k",
        e=edge_id, k=idem_key,
    )
    assert state is not None, f"no runtime_inflight row for ({edge_id}, {idem_key})"
    return state


async def test_claim_skips_succeeded(inflight_db: object) -> None:
    edge, key = "edgeA::cons", "k1"
    async with tx():
        s = current_session()
        await s.execute(text(
            "INSERT INTO runtime_inflight (edge_id, idempotent_key, "
            "data_table, state, attempts) VALUES (:e, :k, 't', 'succeeded', 1)"
        ), {"e": edge, "k": key})
    out = await claim_inflight(
        edge_id=edge, idempotent_key=key, data_table="t",
        worker_id="w", lease_ms=1000,
    )
    assert out.action == "skip"


async def test_claim_skips_review(inflight_db: object) -> None:
    edge, key = "edgeA::cons", "k2"
    async with tx():
        s = current_session()
        await s.execute(text(
            "INSERT INTO runtime_inflight (edge_id, idempotent_key, "
            "data_table, state, attempts) VALUES (:e, :k, 't', 'review', 1)"
        ), {"e": edge, "k": key})
    out = await claim_inflight(
        edge_id=edge, idempotent_key=key, data_table="t",
        worker_id="w", lease_ms=1000,
    )
    assert out.action == "skip"

