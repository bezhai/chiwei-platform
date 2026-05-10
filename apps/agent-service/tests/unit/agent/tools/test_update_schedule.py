"""Test update_schedule tool — Phase 7d tx/emit_tx version."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.tools.update_schedule import _update_schedule_impl
from app.domain.agent_tool_events import ScheduleRevisionCreated


def _make_tx_mock():
    @asynccontextmanager
    async def _fake_tx():
        yield

    return _fake_tx


def _make_emit_tx_mock():
    """Return (async function, captured list)."""
    captured: list = []

    async def _fake_emit_tx(ev):
        captured.append(ev)

    return _fake_emit_tx, captured


@pytest.mark.asyncio
async def test_update_schedule_writes_revision_and_enqueues_outbox():
    fake_emit, captured = _make_emit_tx_mock()

    with patch("app.agent.tools.update_schedule.insert_schedule_revision", new=AsyncMock()) as ins:
        with patch("app.agent.tools.update_schedule.tx", _make_tx_mock()):
            with patch("app.agent.tools.update_schedule.emit_tx", fake_emit):
                out = await _update_schedule_impl(
                    persona_id="chiwei", content="今天...", reason="first draft",
                    created_by="chiwei",
                )

    assert "revision_id" in out
    ins.assert_awaited_once()
    assert len(captured) == 1
    ev = captured[0]
    assert isinstance(ev, ScheduleRevisionCreated)
    assert ev.revision_id == out["revision_id"]
    assert ev.persona_id == "chiwei"


@pytest.mark.asyncio
async def test_update_schedule_rejects_empty():
    fake_emit, captured = _make_emit_tx_mock()

    with patch("app.agent.tools.update_schedule.insert_schedule_revision", new=AsyncMock()) as ins:
        with patch("app.agent.tools.update_schedule.tx", _make_tx_mock()):
            with patch("app.agent.tools.update_schedule.emit_tx", fake_emit):
                out = await _update_schedule_impl(
                    persona_id="chiwei", content=" ", reason="", created_by="chiwei",
                )

    assert "error" in out
    ins.assert_not_awaited()
    assert len(captured) == 0
