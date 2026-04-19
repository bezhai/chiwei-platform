"""arq job: sync_life_state_after_schedule — consume schedule-update events."""

from __future__ import annotations

import logging
from typing import Any

from app.data.queries import get_schedule_revision_by_id
from app.data.session import get_session
from app.life.state_sync import state_only_refresh

logger = logging.getLogger(__name__)


async def sync_life_state_after_schedule(
    ctx: dict[str, Any], revision_id: str
) -> None:
    async with get_session() as s:
        rev = await get_schedule_revision_by_id(s, revision_id)
    if rev is None:
        logger.warning(
            "state_sync: revision %s not found, skip", revision_id
        )
        return

    logger.info(
        "state_sync: refreshing for persona=%s revision=%s",
        rev.persona_id, revision_id,
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
