"""Sister Theater — shared family events for the three sisters.

Generates daily household happenings involving 赤尾, 千凪, 绫奈, and 原智鸿.
Only personality outlines are provided — no interest details — to avoid
biasing the theater toward any persona's hobbies.
"""

from __future__ import annotations

import logging
from datetime import date

from app.agent.core import Agent, AgentConfig, extract_text
from app.life._date_utils import WEEKDAY_CN, get_season

logger = logging.getLogger(__name__)

_THEATER_CFG = AgentConfig("sister_theater", "offline-model", "sister-theater")


async def run_sister_theater(
    target_date: date,
    prev_theater_summary: str = "",
) -> str:
    """Generate 5-6 daily family events for the three sisters.

    All personas share the same theater output — each persona's Writer
    picks the events she cares about from her own perspective.
    """
    result = await Agent(_THEATER_CFG).run(
        messages=[],
        prompt_vars={
            "date": target_date.isoformat(),
            "weekday": WEEKDAY_CN[target_date.weekday()],
            "season": get_season(target_date.month),
            "prev_theater_summary": prev_theater_summary or "（昨天没有小剧场记录）",
        },
    )
    return extract_text(result.content)
