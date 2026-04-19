"""Always-on injection: today_schedule (latest revision)."""

from __future__ import annotations

import logging

from app.data.queries import get_current_schedule
from app.data.session import get_session

logger = logging.getLogger(__name__)


async def build_schedule_section(*, persona_id: str) -> str:
    try:
        async with get_session() as s:
            sr = await get_current_schedule(s, persona_id=persona_id)
    except Exception as e:
        logger.warning("schedule section failed: %s", e)
        return ""
    if sr is None:
        return ""
    return f"今天的安排：\n{sr.content}"
