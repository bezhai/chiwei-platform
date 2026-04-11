"""Dream compression — daily and weekly experience summaries.

Daily dream: today's conversation + glimpse fragments -> first-person review.
Weekly dream: last 7 daily fragments -> weekly review.

Forgetting happens naturally: dozens of fragments compress into one review.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from app.agent.core import Agent, AgentConfig
from app.data.models import ExperienceFragment
from app.data.queries import (
    find_fragments_in_date_range,
    find_persona,
    find_recent_fragments_by_grain,
    insert_fragment,
    list_all_persona_ids,
)
from app.data.session import get_session

_DREAM_DAILY_CFG = AgentConfig("dream_daily", "diary-model", "dream-daily")
_DREAM_WEEKLY_CFG = AgentConfig("dream_weekly", "diary-model", "dream-weekly")

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))


def _text(content) -> str:
    """Extract plain text from LLM response content."""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return (content or "").strip()


# ---------------------------------------------------------------------------
# Daily dream
# ---------------------------------------------------------------------------


async def generate_daily_dream(
    persona_id: str, target_date: date | None = None
) -> ExperienceFragment | None:
    """Generate a daily dream fragment from today's conversation + glimpse fragments."""
    if target_date is None:
        target_date = date.today() - timedelta(days=1)

    day_start = datetime(
        target_date.year, target_date.month, target_date.day, tzinfo=_CST
    )
    day_end = day_start + timedelta(days=1)

    async with get_session() as s:
        today_frags = await find_fragments_in_date_range(
            s, persona_id, target_date, target_date, grains=["conversation", "glimpse"]
        )
    if not today_frags:
        logger.info(
            "[%s] No fragments for %s, skip daily dream", persona_id, target_date
        )
        return None

    async with get_session() as s:
        persona_obj = await find_persona(s, persona_id)
        recent_dailies = await find_recent_fragments_by_grain(
            s, persona_id, "daily", limit=3
        )

    persona_name = persona_obj.display_name if persona_obj else persona_id
    persona_lite = persona_obj.persona_lite if persona_obj else ""
    today_text = "\n\n---\n\n".join(f.content for f in today_frags)
    recent_text = (
        "\n\n---\n\n".join(f.content for f in reversed(recent_dailies))
        if recent_dailies
        else "（前几天没有做梦）"
    )

    result = await Agent(_DREAM_DAILY_CFG).run(
        prompt_vars={
            "persona_name": persona_name,
            "persona_lite": persona_lite,
            "date": target_date.isoformat(),
            "today_fragments": today_text,
            "recent_dreams": recent_text,
        },
        messages=[HumanMessage(content="回忆今天发生的事")],
    )
    content = _text(result.content)

    if not content:
        logger.warning(
            "[%s] Daily dream LLM returned empty for %s", persona_id, target_date
        )
        return None

    fragment = ExperienceFragment(
        persona_id=persona_id,
        grain="daily",
        content=content,
        time_start=int(day_start.timestamp() * 1000),
        time_end=int(day_end.timestamp() * 1000),
    )
    async with get_session() as s:
        saved = await insert_fragment(s, fragment)
    logger.info(
        "[%s] Daily dream created: id=%s, date=%s, len=%d",
        persona_id,
        saved.id,
        target_date,
        len(content),
    )
    return saved


# ---------------------------------------------------------------------------
# Weekly dream
# ---------------------------------------------------------------------------


async def generate_weekly_dream(
    persona_id: str, target_date: date | None = None
) -> ExperienceFragment | None:
    """Generate a weekly dream fragment from the last 7 daily fragments."""
    if target_date is None:
        target_date = date.today()

    async with get_session() as s:
        dailies = await find_recent_fragments_by_grain(s, persona_id, "daily", limit=7)
    if not dailies:
        logger.info("[%s] No daily fragments for weekly dream, skip", persona_id)
        return None

    async with get_session() as s:
        persona_obj = await find_persona(s, persona_id)

    persona_name = persona_obj.display_name if persona_obj else persona_id
    persona_lite = persona_obj.persona_lite if persona_obj else ""
    dailies_text = "\n\n---\n\n".join(f.content for f in reversed(dailies))

    result = await Agent(_DREAM_WEEKLY_CFG).run(
        prompt_vars={
            "persona_name": persona_name,
            "persona_lite": persona_lite,
            "dailies": dailies_text,
        },
        messages=[HumanMessage(content="回顾这一周")],
    )
    content = _text(result.content)

    if not content:
        logger.warning("[%s] Weekly dream LLM returned empty", persona_id)
        return None

    week_end = datetime(
        target_date.year, target_date.month, target_date.day, tzinfo=_CST
    )
    week_start = week_end - timedelta(days=7)

    fragment = ExperienceFragment(
        persona_id=persona_id,
        grain="weekly",
        content=content,
        time_start=int(week_start.timestamp() * 1000),
        time_end=int(week_end.timestamp() * 1000),
    )
    async with get_session() as s:
        saved = await insert_fragment(s, fragment)
    logger.info(
        "[%s] Weekly dream created: id=%s, len=%d", persona_id, saved.id, len(content)
    )
    return saved


# ---------------------------------------------------------------------------
# Cron entry points (called by workers)
# ---------------------------------------------------------------------------


async def run_daily_dreams() -> None:
    """Generate yesterday's daily dream for every persona."""
    yesterday = date.today() - timedelta(days=1)
    async with get_session() as s:
        persona_ids = await list_all_persona_ids(s)
    for pid in persona_ids:
        try:
            await generate_daily_dream(pid, yesterday)
        except Exception:
            logger.exception("[%s] Daily dream failed", pid)


async def run_weekly_dreams() -> None:
    """Generate weekly dream (Monday) for every persona."""
    today = date.today()
    async with get_session() as s:
        persona_ids = await list_all_persona_ids(s)
    for pid in persona_ids:
        try:
            await generate_weekly_dream(pid, today)
        except Exception:
            logger.exception("[%s] Weekly dream failed", pid)
