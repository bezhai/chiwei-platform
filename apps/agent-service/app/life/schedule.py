"""Schedule — Agent Team daily plan generation.

Pipeline: Wild Agents (parallel) + Search Anchors + Sister Theater
        → Curator (persona filter) → Writer → Critic

Monthly and weekly plans have been removed. Daily plans are generated
directly from diverse external stimuli instead of narrowing funnels.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from app.agent.core import Agent, AgentConfig, extract_text
from app.agent.tools.search import search_web
from app.data import queries as Q
from app.data.models import AkaoSchedule
from app.data.session import get_session
from app.infra.config import settings
from app.life._date_utils import CST, WEEKDAY_CN, get_season
from app.life.sister_theater import run_sister_theater
from app.life.wild_agents import run_wild_agents
from app.memory._persona import load_persona

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent configs
# ---------------------------------------------------------------------------

_CURATOR_CFG = AgentConfig("daily_curator", "offline-model", "daily-curator")
_WRITER_CFG = AgentConfig("schedule_daily_writer", "offline-model", "schedule-writer")
_CRITIC_CFG = AgentConfig("schedule_daily_critic", "offline-model", "schedule-critic")


def _schedule_model() -> str:
    """Model used for schedule generation (shares diary_model config)."""
    return settings.diary_model


# ---------------------------------------------------------------------------
# Search anchors (factual reality anchoring)
# ---------------------------------------------------------------------------


async def _fetch_search_anchors(target_date: date) -> str:
    """Fetch 3-5 factual search results to anchor the schedule in reality.

    Queries are system-constructed (not LLM-generated).
    """
    date_str = target_date.isoformat()
    month = target_date.month
    queries = [
        f"杭州 {date_str} 天气",
        f"{target_date.year}年{month}月新番 本周更新",
        "杭州 老城区 最近 新开 关门 展览",
    ]

    results = []
    for q in queries:
        try:
            text = await search_web.ainvoke({"query": q, "num": 2})
            if text and text != "未搜索到相关结果":
                results.append(f"[{q}]\n{text[:500]}")
        except Exception as e:
            logger.warning("Search anchor '%s' failed: %s", q, e)

    return "\n\n".join(results) if results else "（搜索锚点获取失败）"


# ---------------------------------------------------------------------------
# Shared pipeline (persona-independent, run once per day)
# ---------------------------------------------------------------------------


async def _run_shared_pipeline(target_date: date) -> tuple[str, str, str]:
    """Run shared steps in parallel: wild agents + search anchors + sister theater.

    Returns (wild_materials, search_anchors, theater_text).
    """
    wild_task = run_wild_agents(target_date)
    search_task = _fetch_search_anchors(target_date)
    theater_task = run_sister_theater(target_date)

    results = await asyncio.gather(wild_task, search_task, theater_task, return_exceptions=True)

    wild = results[0] if not isinstance(results[0], Exception) else ""
    anchors = results[1] if not isinstance(results[1], Exception) else ""
    theater = results[2] if not isinstance(results[2], Exception) else ""

    for i, label in enumerate(["Wild agents", "Search anchors", "Sister theater"]):
        if isinstance(results[i], Exception):
            logger.warning("%s failed: %s", label, results[i])

    return wild, anchors, theater


# ---------------------------------------------------------------------------
# Per-persona agent helpers
# ---------------------------------------------------------------------------


async def _run_curator(persona_lite: str, all_materials: str) -> str:
    """Curator Agent: filter materials through persona's perspective."""
    result = await Agent(_CURATOR_CFG).run(
        messages=[],
        prompt_vars={"persona_lite": persona_lite, "all_materials": all_materials},
    )
    return extract_text(result.content)


async def _run_writer(
    persona_core: str,
    curated_materials: str,
    theater: str,
    yesterday_journal: str,
    target_date: date,
    previous_output: str = "",
    critic_feedback: str = "",
) -> str:
    """Writer Agent: compose the daily journal/schedule."""
    result = await Agent(_WRITER_CFG).run(
        messages=[],
        prompt_vars={
            "persona_core": persona_core,
            "date": target_date.isoformat(),
            "weekday": WEEKDAY_CN[target_date.weekday()],
            "is_weekend": "周末！" if target_date.weekday() >= 5 else "",
            "yesterday_journal": yesterday_journal,
            "curated_materials": curated_materials,
            "theater": theater,
            "previous_output": previous_output,
            "critic_feedback": critic_feedback,
        },
    )
    return extract_text(result.content)


async def _run_critic(
    schedule_text: str,
    recent_schedules_text: str,
    persona_name: str = "",
) -> str:
    """Critic Agent: review quality, return PASS or revision notes."""
    result = await Agent(_CRITIC_CFG).run(
        messages=[],
        prompt_vars={
            "persona_name": persona_name,
            "today_schedule": schedule_text,
            "recent_schedules": recent_schedules_text,
        },
    )
    return extract_text(result.content)


# ---------------------------------------------------------------------------
# Recent schedules helper
# ---------------------------------------------------------------------------


async def _get_recent_daily_schedules(
    before_date: date, persona_id: str, count: int = 3
) -> list[AkaoSchedule]:
    """Fetch recent daily schedules before a date (for Critic context)."""
    async with get_session() as s:
        results = await Q.list_schedules(
            s, plan_type="daily", persona_id=persona_id,
            active_only=True, limit=count + 5,
        )
    return [sched for sched in results if sched.period_start < before_date.isoformat()][:count]


def _format_recent_schedules(schedules: list[AkaoSchedule]) -> str:
    if not schedules:
        return "（没有前几天的日程）"
    return "\n\n---\n\n".join(
        f"[{sched.period_start}]\n{sched.content}" for sched in schedules
    )


# ---------------------------------------------------------------------------
# Per-persona pipeline
# ---------------------------------------------------------------------------


async def _run_persona_pipeline(
    persona_id: str,
    target_date: date,
    wild_materials: str,
    search_anchors: str,
    theater: str,
) -> str | None:
    """Per-persona pipeline: curator → writer → critic loop.

    Returns the final schedule text, or None on failure.
    """
    date_str = target_date.isoformat()

    # Skip if already generated
    async with get_session() as s:
        existing = await Q.find_plan_for_period(s, "daily", date_str, date_str, persona_id)
    if existing:
        logger.info("[%s] Daily plan already exists for %s, skip", persona_id, date_str)
        return existing.content

    pc = await load_persona(persona_id)

    # Combine materials for curator input
    all_materials = wild_materials
    if search_anchors:
        all_materials += f"\n\n--- 真实搜索锚点 ---\n{search_anchors}"

    # Yesterday's journal
    async with get_session() as s:
        recent_dailies = await Q.find_recent_fragments_by_grain(
            s, persona_id, "daily", limit=1
        )
    yesterday_journal = recent_dailies[0].content if recent_dailies else "（昨天没有写日志）"

    # Recent schedules for critic
    recent = await _get_recent_daily_schedules(target_date, persona_id)
    recent_schedules_text = _format_recent_schedules(recent)

    # Curator: filter materials through persona lens
    try:
        curated = await _run_curator(pc.persona_lite, all_materials)
    except Exception as e:
        logger.warning("[%s] Curator failed, using raw materials: %s", persona_id, e)
        curated = all_materials[:2000]

    # Writer → Critic loop (max 3 attempts)
    feedback = ""
    previous_output = ""
    schedule_text = ""

    for attempt in range(3):
        schedule_text = await _run_writer(
            persona_core=pc.persona_core,
            curated_materials=curated,
            theater=theater,
            yesterday_journal=yesterday_journal,
            target_date=target_date,
            previous_output=previous_output,
            critic_feedback=feedback,
        )

        critic_result = await _run_critic(
            schedule_text=schedule_text,
            recent_schedules_text=recent_schedules_text,
            persona_name=pc.display_name,
        )

        if critic_result.strip().upper().startswith("PASS"):
            logger.info("[%s] Daily plan passed critic on attempt %d", persona_id, attempt + 1)
            break

        logger.info(
            "[%s] Critic rejected (attempt %d): %s",
            persona_id, attempt + 1, critic_result[:100],
        )
        previous_output = schedule_text
        feedback = critic_result

    if not schedule_text:
        logger.warning("[%s] Pipeline produced empty daily plan for %s", persona_id, date_str)
        return None

    # Persist
    async with get_session() as s:
        await Q.upsert_schedule(
            s,
            AkaoSchedule(
                plan_type="daily",
                period_start=date_str,
                period_end=date_str,
                persona_id=persona_id,
                content=schedule_text,
                model=_schedule_model(),
            ),
        )

    logger.info("[%s] Daily plan generated for %s: %d chars", persona_id, date_str, len(schedule_text))
    return schedule_text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_daily_plan(
    persona_id: str, target_date: date | None = None
) -> str | None:
    """Generate a daily plan for a single persona (admin trigger).

    Runs the full pipeline including shared steps.
    """
    if target_date is None:
        target_date = datetime.now(CST).date()

    wild, anchors, theater = await _run_shared_pipeline(target_date)
    return await _run_persona_pipeline(persona_id, target_date, wild, anchors, theater)


async def generate_all_daily_plans(target_date: date | None = None) -> None:
    """Generate daily plans for all personas (cron job).

    Shared steps (wild agents + search + theater) run once.
    Per-persona steps (curator + writer + critic) run for each persona.
    """
    if target_date is None:
        target_date = datetime.now(CST).date()

    logger.info("Generating daily plans for all personas: %s", target_date.isoformat())

    wild, anchors, theater = await _run_shared_pipeline(target_date)

    async with get_session() as s:
        persona_ids = await Q.list_all_persona_ids(s)

    for persona_id in persona_ids:
        try:
            await _run_persona_pipeline(persona_id, target_date, wild, anchors, theater)
        except Exception:
            logger.exception("[%s] daily plan generation failed", persona_id)


# ---------------------------------------------------------------------------
# Schedule context builder (for injecting into chat system prompt)
# ---------------------------------------------------------------------------


async def build_schedule_context(persona_id: str) -> str:
    """Build the current daily schedule context for system prompt injection.

    Returns empty string if no daily plan exists for today.
    """
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")

    async with get_session() as s:
        daily = await Q.find_plan_for_period(s, "daily", today, today, persona_id)

    if not daily:
        return ""
    return daily.content
