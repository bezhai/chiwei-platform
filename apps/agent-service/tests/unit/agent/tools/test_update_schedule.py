"""Test update_schedule tool — Phase 6 v4 emit-based version."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.agent.tools.update_schedule import _update_schedule_impl
from app.domain.agent_tool_events import ScheduleRevisionCreated


@pytest.mark.asyncio
async def test_update_schedule_writes_revision_and_emits():
    with patch("app.agent.tools.update_schedule.insert_schedule_revision", new=AsyncMock()) as ins:
        with patch("app.agent.tools.update_schedule.emit", new_callable=AsyncMock) as em:
            out = await _update_schedule_impl(
                persona_id="chiwei", content="今天...", reason="first draft",
                created_by="chiwei",
            )
    assert "revision_id" in out
    ins.assert_awaited_once()
    em.assert_awaited_once()
    emitted = em.await_args.args[0]
    assert isinstance(emitted, ScheduleRevisionCreated)
    assert emitted.revision_id == out["revision_id"]
    assert emitted.persona_id == "chiwei"


@pytest.mark.asyncio
async def test_update_schedule_rejects_empty():
    with patch("app.agent.tools.update_schedule.insert_schedule_revision", new=AsyncMock()) as ins:
        with patch("app.agent.tools.update_schedule.emit", new_callable=AsyncMock) as em:
            out = await _update_schedule_impl(
                persona_id="chiwei", content=" ", reason="", created_by="chiwei",
            )
    assert "error" in out
    ins.assert_not_awaited()
    em.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_schedule_returns_success_even_if_emit_fails():
    """emit failure must not lose the already-committed revision."""
    with patch("app.agent.tools.update_schedule.insert_schedule_revision", new=AsyncMock()):
        with patch(
            "app.agent.tools.update_schedule.emit",
            new=AsyncMock(side_effect=RuntimeError("mq down")),
        ):
            out = await _update_schedule_impl(
                persona_id="chiwei", content="test", reason="r",
                created_by="chiwei",
            )
    assert "revision_id" in out
    assert "error" not in out
