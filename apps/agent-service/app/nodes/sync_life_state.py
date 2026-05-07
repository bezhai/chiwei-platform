"""sync_life_state_node — react to ScheduleRevisionCreated by re-evaluating life state.

Replaces app/workers/state_sync_worker.py (arq worker). Wired in
app/wiring/life_dataflow.py via wire(ScheduleRevisionCreated).durable();
bind to agent-service in app/deployment.py so the durable consumer
runs in the main process. arq runtime is retired in Task 8.
"""
from __future__ import annotations

import logging

from app.data.queries import get_schedule_revision_by_id
from app.data.session import get_session
from app.domain.agent_tool_events import ScheduleRevisionCreated
from app.life.state_sync import state_only_refresh
from app.runtime import node

logger = logging.getLogger(__name__)


@node
async def sync_life_state_node(e: ScheduleRevisionCreated) -> None:
    async with get_session() as s:
        rev = await get_schedule_revision_by_id(s, e.revision_id)
    if rev is None:
        logger.warning(
            "state_sync: revision %s not found, skip", e.revision_id
        )
        return

    logger.info(
        "state_sync: refreshing for persona=%s revision=%s",
        rev.persona_id, e.revision_id,
    )
    result = await state_only_refresh(
        persona_id=rev.persona_id,
        new_schedule_content=rev.content,
    )
    if result is None:
        logger.info(
            "state_sync: no refresh (LLM decided unchanged or no prev state)"
        )
    elif result.ok:
        logger.info(
            "state_sync: committed life state id=%s refresh=%s",
            result.life_state_id, result.is_refresh,
        )
    else:
        logger.warning("state_sync: commit failed: %s", result.error)
