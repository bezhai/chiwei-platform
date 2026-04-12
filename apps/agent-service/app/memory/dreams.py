"""Dream compression — daily and weekly experience summaries.

Daily dream: today's conversation + glimpse fragments -> first-person review.
Weekly dream: last 7 daily fragments -> weekly review.

Forgetting happens naturally: dozens of fragments compress into one review.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from app.agent.core import Agent, AgentConfig, extract_text
from app.data.models import ExperienceFragment
from app.data.queries import (
    find_fragments_in_date_range,
    find_recent_fragments_by_grain,
    insert_fragment,
    list_all_persona_ids,
)
from app.data.session import get_session
from app.memory._persona import load_persona

_DREAM_DAILY_CFG = AgentConfig("dream_daily", "diary-model", "dream-daily")
_DREAM_WEEKLY_CFG = AgentConfig("dream_weekly", "diary-model", "dream-weekly")

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# Daily dream
# ---------------------------------------------------------------------------


async def generate_daily_dream(
    persona_id: str, target_date: date | None = None
) -> ExperienceFragment | None:
    """Generate a daily dream fragment from today's conversation + glimpse fragments."""
    if target_date is None:
        target_date = datetime.now(_CST).date() - timedelta(days=1)

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

    pc = await load_persona(persona_id)
    async with get_session() as s:
        recent_dailies = await find_recent_fragments_by_grain(
            s, persona_id, "daily", limit=3
        )

    today_text = "\n\n---\n\n".join(f.content for f in today_frags)
    recent_text = (
        "\n\n---\n\n".join(f.content for f in reversed(recent_dailies))
        if recent_dailies
        else "（前几天没有做梦）"
    )

    result = await Agent(_DREAM_DAILY_CFG).run(
        prompt_vars={
            "persona_name": pc.display_name,
            "persona_lite": pc.persona_lite,
            "date": target_date.isoformat(),
            "today_fragments": today_text,
            "recent_dreams": recent_text,
        },
        messages=[HumanMessage(content="回忆今天发生的事")],
    )
    content = extract_text(result.content)

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
        target_date = datetime.now(_CST).date()

    async with get_session() as s:
        dailies = await find_recent_fragments_by_grain(s, persona_id, "daily", limit=7)
    if not dailies:
        logger.info("[%s] No daily fragments for weekly dream, skip", persona_id)
        return None

    pc = await load_persona(persona_id)
    dailies_text = "\n\n---\n\n".join(f.content for f in reversed(dailies))

    result = await Agent(_DREAM_WEEKLY_CFG).run(
        prompt_vars={
            "persona_name": pc.display_name,
            "persona_lite": pc.persona_lite,
            "dailies": dailies_text,
        },
        messages=[HumanMessage(content="回顾这一周")],
    )
    content = extract_text(result.content)

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
    yesterday = datetime.now(_CST).date() - timedelta(days=1)
    async with get_session() as s:
        persona_ids = await list_all_persona_ids(s)
    for pid in persona_ids:
        try:
            await generate_daily_dream(pid, yesterday)
        except Exception:
            logger.exception("[%s] Daily dream failed", pid)


async def run_weekly_dreams() -> None:
    """Generate weekly dream (Monday) for every persona."""
    today = datetime.now(_CST).date()
    async with get_session() as s:
        persona_ids = await list_all_persona_ids(s)
    for pid in persona_ids:
        try:
            await generate_weekly_dream(pid, today)
        except Exception:
            logger.exception("[%s] Weekly dream failed", pid)
