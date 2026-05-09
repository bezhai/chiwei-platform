"""Phase 7b Gap 8: outbox dispatcher_loop unit tests.

Mocks `emit` / `deserialize_data` / `bind_propagation_from_payload`;
NEVER mocks mq.* — dispatcher's only output is a call to emit().
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.outbox_dispatcher import _drain_once

pytestmark = pytest.mark.integration


async def _seed(*, app="agent-service", lane=None,
                data_type="x.Y", payload=None, trace_id="tr"):
    payload = payload or {"id": "x"}
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO runtime_outbox "
            "(data_type, payload_json, origin_app, lane, trace_id) "
            "VALUES (:dt, CAST(:pj AS jsonb), :a, :l, :t)"
        ), {"dt": data_type, "pj": json.dumps(payload), "a": app, "l": lane, "t": trace_id})
        await s.commit()


async def test_drain_dispatches_one_pending_row(outbox_db: object) -> None:
    await _seed()
    fake_data = object()
    with patch("app.runtime.outbox_dispatcher.emit", new=AsyncMock()) as e, \
         patch("app.runtime.outbox_dispatcher.deserialize_data",
               return_value=fake_data):
        await _drain_once(app="agent-service", lane=None)
    e.assert_awaited_once_with(fake_data)
    async with get_session() as s:
        state = (await s.execute(text(
            "SELECT state FROM runtime_outbox LIMIT 1"
        ))).scalar()
    assert state == "dispatched"


async def test_drain_skips_rows_for_other_app(outbox_db: object) -> None:
    await _seed(app="other")
    with patch("app.runtime.outbox_dispatcher.emit", new=AsyncMock()) as e:
        await _drain_once(app="agent-service", lane=None)
    e.assert_not_awaited()


async def test_drain_skips_rows_for_other_lane(outbox_db: object) -> None:
    await _seed(lane="feat-x")
    with patch("app.runtime.outbox_dispatcher.emit", new=AsyncMock()) as e:
        await _drain_once(app="agent-service", lane=None)  # prod dispatcher
    e.assert_not_awaited()


async def test_drain_lane_match_succeeds(outbox_db: object) -> None:
    await _seed(lane="feat-x")
    with patch("app.runtime.outbox_dispatcher.emit", new=AsyncMock()) as e, \
         patch("app.runtime.outbox_dispatcher.deserialize_data",
               return_value=object()):
        await _drain_once(app="agent-service", lane="feat-x")
    e.assert_awaited_once()


async def test_emit_failure_increments_attempts(outbox_db: object) -> None:
    await _seed()
    with patch("app.runtime.outbox_dispatcher.emit",
               new=AsyncMock(side_effect=RuntimeError("boom"))), \
         patch("app.runtime.outbox_dispatcher.deserialize_data",
               return_value=object()):
        await _drain_once(app="agent-service", lane=None)
    async with get_session() as s:
        row = (await s.execute(text(
            "SELECT state, attempts, last_error FROM runtime_outbox LIMIT 1"
        ))).mappings().first()
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    assert "boom" in (row["last_error"] or "")


async def test_propagation_bound_before_emit(outbox_db: object) -> None:
    """bind_propagation_from_payload must run BEFORE emit() so consumers
    see the right trace_id/lane."""
    seen = {}

    async def _fake_emit(data):
        from app.api.middleware import lane_var, trace_id_var
        seen["lane"] = lane_var.get()
        seen["trace_id"] = trace_id_var.get()

    await _seed(lane="feat-x", trace_id="tr-99")
    with patch("app.runtime.outbox_dispatcher.emit", side_effect=_fake_emit), \
         patch("app.runtime.outbox_dispatcher.deserialize_data",
               return_value=object()):
        await _drain_once(app="agent-service", lane="feat-x")
    assert seen["lane"] == "feat-x"
    assert seen["trace_id"] == "tr-99"
