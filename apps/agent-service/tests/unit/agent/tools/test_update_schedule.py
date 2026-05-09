"""Test update_schedule tool — Phase 7b outbox-based version."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.tools.update_schedule import _update_schedule_impl
from app.domain.agent_tool_events import ScheduleRevisionCreated


def _make_session_mock():
    """Return an AsyncMock that behaves as 'async with get_session() as s'."""
    session = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, session


def _make_transactional_emit_mock():
    """Return a (patch-target, captured_events) pair.

    The patch replaces transactional_emit with an async context manager that
    captures every data object passed to emitter.append().
    """
    captured: list = []

    @asynccontextmanager
    async def _fake_transactional_emit(_session):
        emitter = MagicMock()
        emitter.append = AsyncMock(side_effect=lambda ev: captured.append(ev) or None)
        yield emitter

    return _fake_transactional_emit, captured


@pytest.mark.asyncio
async def test_update_schedule_writes_revision_and_enqueues_outbox():
    fake_te, captured = _make_transactional_emit_mock()
    session_ctx, _session = _make_session_mock()

    with patch("app.agent.tools.update_schedule.insert_schedule_revision", new=AsyncMock()) as ins:
        with patch("app.agent.tools.update_schedule.get_session", return_value=session_ctx):
            with patch("app.agent.tools.update_schedule.transactional_emit", fake_te):
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
    fake_te, captured = _make_transactional_emit_mock()
    session_ctx, _session = _make_session_mock()

    with patch("app.agent.tools.update_schedule.insert_schedule_revision", new=AsyncMock()) as ins:
        with patch("app.agent.tools.update_schedule.get_session", return_value=session_ctx):
            with patch("app.agent.tools.update_schedule.transactional_emit", fake_te):
                out = await _update_schedule_impl(
                    persona_id="chiwei", content=" ", reason="", created_by="chiwei",
                )

    assert "error" in out
    ins.assert_not_awaited()
    assert len(captured) == 0
