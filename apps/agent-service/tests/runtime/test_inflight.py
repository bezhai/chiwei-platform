"""Contract tests for runtime/inflight.py — Gap 7.1 state machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.inflight import (
    claim_inflight,
    edge_id_for,
    mark_failed,
    mark_history_backfill,
    mark_succeeded,
)

pytestmark = pytest.mark.integration


async def _row(edge_id: str, idem_key: str) -> dict:
    async with get_session() as s:
        r = await s.execute(text(
            "SELECT state, attempts, locked_until, worker_id, last_error, trace_id "
            "FROM runtime_inflight WHERE edge_id=:e AND idempotent_key=:k"
        ), {"e": edge_id, "k": idem_key})
        return dict(r.mappings().one())


class TestEdgeIdHelper:
    def test_combines_qualnames(self) -> None:
        assert edge_id_for("Foo", "consumer") == "Foo::consumer"
        assert edge_id_for("mod.Foo", "mod.consumer") == "mod.Foo::mod.consumer"


class TestClaimInflightFresh:
    async def test_first_time_creates_processing_row_and_returns_fresh(
        self, inflight_db: object
    ) -> None:
        outcome = await claim_inflight(
            edge_id="E::c", idempotent_key="k1", data_table="foo",
            worker_id="host:1", lease_ms=60_000, trace_id="t1",
        )
        assert outcome.action == "run"
        assert outcome.attempts == 1
        assert outcome.fresh is True

        row = await _row("E::c", "k1")
        assert row["state"] == "processing"
        assert row["attempts"] == 1
        assert row["worker_id"] == "host:1"
        assert row["trace_id"] == "t1"
        assert row["locked_until"] is not None


class TestClaimInflightSucceededSkip:
    async def test_succeeded_returns_skip(self, inflight_db: object) -> None:
        async with get_session() as s:
            await s.execute(text(
                "INSERT INTO runtime_inflight "
                "(edge_id, idempotent_key, data_table, state, attempts) "
                "VALUES ('E::c', 'k1', 'foo', 'succeeded', 1)"
            ))
        outcome = await claim_inflight(
            edge_id="E::c", idempotent_key="k1", data_table="foo",
            worker_id="host:1", lease_ms=60_000,
        )
        assert outcome.action == "skip"
        assert outcome.fresh is False


class TestClaimInflightLeaseLive:
    async def test_processing_with_live_lease_returns_skip(
        self, inflight_db: object
    ) -> None:
        future = datetime.now(UTC) + timedelta(minutes=5)
        async with get_session() as s:
            await s.execute(text(
                "INSERT INTO runtime_inflight "
                "(edge_id, idempotent_key, data_table, state, attempts, "
                " locked_until, worker_id) "
                "VALUES ('E::c', 'k1', 'foo', 'processing', 1, :lu, 'host:other')"
            ), {"lu": future})

        outcome = await claim_inflight(
            edge_id="E::c", idempotent_key="k1", data_table="foo",
            worker_id="host:1", lease_ms=60_000,
        )
        assert outcome.action == "skip"

        row = await _row("E::c", "k1")
        assert row["worker_id"] == "host:other"  # 不接管


class TestClaimInflightLeaseExpired:
    async def test_processing_with_expired_lease_takes_over(
        self, inflight_db: object
    ) -> None:
        past = datetime.now(UTC) - timedelta(minutes=1)
        async with get_session() as s:
            await s.execute(text(
                "INSERT INTO runtime_inflight "
                "(edge_id, idempotent_key, data_table, state, attempts, "
                " locked_until, worker_id) "
                "VALUES ('E::c', 'k1', 'foo', 'processing', 2, :lu, 'host:dead')"
            ), {"lu": past})

        outcome = await claim_inflight(
            edge_id="E::c", idempotent_key="k1", data_table="foo",
            worker_id="host:new", lease_ms=60_000,
        )
        assert outcome.action == "run"
        assert outcome.attempts == 3
        assert outcome.fresh is False

        row = await _row("E::c", "k1")
        assert row["worker_id"] == "host:new"
        assert row["state"] == "processing"


class TestClaimInflightFailedResume:
    async def test_failed_resumes_as_processing(self, inflight_db: object) -> None:
        async with get_session() as s:
            await s.execute(text(
                "INSERT INTO runtime_inflight "
                "(edge_id, idempotent_key, data_table, state, attempts, last_error) "
                "VALUES ('E::c', 'k1', 'foo', 'failed', 1, 'boom')"
            ))

        outcome = await claim_inflight(
            edge_id="E::c", idempotent_key="k1", data_table="foo",
            worker_id="host:1", lease_ms=60_000,
        )
        assert outcome.action == "run"
        assert outcome.attempts == 2
        assert outcome.fresh is False


class TestMarkSucceeded:
    async def test_clears_lease_and_worker(self, inflight_db: object) -> None:
        await claim_inflight(
            edge_id="E::c", idempotent_key="k1", data_table="foo",
            worker_id="host:1", lease_ms=60_000,
        )
        await mark_succeeded(edge_id="E::c", idempotent_key="k1")

        row = await _row("E::c", "k1")
        assert row["state"] == "succeeded"
        assert row["locked_until"] is None
        assert row["worker_id"] is None


class TestMarkFailed:
    async def test_records_error_and_clears_lease(self, inflight_db: object) -> None:
        await claim_inflight(
            edge_id="E::c", idempotent_key="k1", data_table="foo",
            worker_id="host:1", lease_ms=60_000,
        )
        await mark_failed(
            edge_id="E::c", idempotent_key="k1",
            last_error="RuntimeError(boom)",
        )
        row = await _row("E::c", "k1")
        assert row["state"] == "failed"
        assert "boom" in row["last_error"]
        assert row["locked_until"] is None
        assert row["worker_id"] is None


class TestMarkHistoryBackfill:
    async def test_overrides_processing_to_succeeded(
        self, inflight_db: object
    ) -> None:
        # Simulate fresh claim having just inserted a processing row,
        # then handler discovers existing Data row → backfill.
        await claim_inflight(
            edge_id="E::c", idempotent_key="k1", data_table="foo",
            worker_id="host:1", lease_ms=60_000, trace_id="t",
        )
        await mark_history_backfill(
            edge_id="E::c", idempotent_key="k1", data_table="foo",
        )
        row = await _row("E::c", "k1")
        assert row["state"] == "succeeded"
        assert row["attempts"] == 0
        assert row["trace_id"] == "backfill"


class TestEdgeIdIsolation:
    """Same idempotent_key + 不同 edge_id (consumer A 与 consumer B)
    必须独立 state — consumer A succeeded 不让 consumer B 被 dedup。"""

    async def test_succeeded_on_one_edge_does_not_skip_another(
        self, inflight_db: object
    ) -> None:
        outcome_a = await claim_inflight(
            edge_id="E::cA", idempotent_key="k1", data_table="foo",
            worker_id="host:1", lease_ms=60_000,
        )
        await mark_succeeded(edge_id="E::cA", idempotent_key="k1")

        outcome_b = await claim_inflight(
            edge_id="E::cB", idempotent_key="k1", data_table="foo",
            worker_id="host:1", lease_ms=60_000,
        )
        assert outcome_a.action == "run"
        assert outcome_b.action == "run"
        assert outcome_b.fresh is True  # B 是首次创建
