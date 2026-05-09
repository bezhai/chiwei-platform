"""Phase 6 v4 Gap 4: sync_life_state_node replaces arq state_sync_worker."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.agent_tool_events import ScheduleRevisionCreated
from app.nodes.sync_life_state import sync_life_state_node


@pytest.mark.asyncio
async def test_sync_life_state_calls_refresh_with_revision_data():
    revision = MagicMock(persona_id="akao-001", content="周五加班")

    async def fake_get(rid):
        assert rid == "sr_1"
        return revision

    fake_refresh = AsyncMock(return_value=MagicMock(ok=True, life_state_id="ls_1", is_refresh=True))

    with patch("app.nodes.sync_life_state.get_schedule_revision_by_id", new=fake_get), \
         patch("app.nodes.sync_life_state.state_only_refresh", new=fake_refresh):
        await sync_life_state_node(
            ScheduleRevisionCreated(revision_id="sr_1", persona_id="akao-001")
        )

    fake_refresh.assert_awaited_once_with(
        persona_id="akao-001",
        new_schedule_content="周五加班",
    )


@pytest.mark.asyncio
async def test_sync_life_state_skips_when_revision_not_found():
    async def fake_get(_rid):
        return None

    fake_refresh = AsyncMock()

    with patch("app.nodes.sync_life_state.get_schedule_revision_by_id", new=fake_get), \
         patch("app.nodes.sync_life_state.state_only_refresh", new=fake_refresh):
        await sync_life_state_node(
            ScheduleRevisionCreated(revision_id="sr_missing", persona_id="akao-001")
        )

    fake_refresh.assert_not_awaited()
