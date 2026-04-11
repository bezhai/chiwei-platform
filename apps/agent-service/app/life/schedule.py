"""Schedule -- three-tier plan generation (monthly -> weekly -> daily).

Daily uses an Ideation -> Writer -> Critic Agent pipeline.
Monthly and weekly are single-Agent generation.

All plans are stored via ``upsert_schedule`` in the ``akao_schedule`` table.
Each layer inherits context from the layer above.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from app.agent.core import Agent, AgentConfig
from app.agent.tools.search import search_web
from app.data import queries as Q
from app.data.models import AkaoSchedule
from app.data.session import get_session
from app.infra.config import settings

_IDEATION_CFG = AgentConfig(
    "schedule_daily_ideation", "offline-model", "schedule-ideation",
    recursion_limit=42,  # multi-step web search needs more steps
)
_WRITER_CFG = AgentConfig("schedule_daily_writer", "offline-model", "schedule-writer")
_CRITIC_CFG = AgentConfig("schedule_daily_critic", "offline-model", "schedule-critic")
_MONTHLY_CFG = AgentConfig("schedule_monthly", "offline-model", "schedule-monthly")
_WEEKLY_CFG = AgentConfig("schedule_weekly", "offline-model", "schedule-weekly")

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_SEASON_MAP = {
    (3, 4, 5): "春天",
    (6, 7, 8): "夏天",
    (9, 10, 11): "秋天",
    (12, 1, 2): "冬天",
}


def _get_season(month: int) -> str:
    for months, name in _SEASON_MAP.items():
        if month in months:
            return name
    return "未知"


def _schedule_model() -> str:
    """Model used for schedule generation (shares diary_model config)."""
    return settings.diary_model


def _extract_text(content: object) -> str:
    """Extract plain text from LLM response content."""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return content or ""


# ---------------------------------------------------------------------------
# Daily pipeline helpers
# ---------------------------------------------------------------------------


async def _get_recent_daily_schedules(
    before_date: date, persona_id: str, count: int = 3
) -> list[AkaoSchedule]:
    """Fetch recent daily schedules before a date (for Ideation and Critic)."""
    async with get_session() as s:
        results = await Q.list_schedules(
            s,
            plan_type="daily",
            persona_id=persona_id,
            active_only=True,
            limit=count + 5,
        )
    return [sched for sched in results if sched.period_start < before_date.isoformat()][
        :count
    ]


async def _run_ideation(
    recent_schedules_text: str,
    target_date: date,
) -> str:
    """Ideation Agent: brainstorm materials from the outside world.

    Uses web search tool. Runs without persona core to avoid interest bias.
    """
    season = _get_season(target_date.month)
    weekday = _WEEKDAY_CN[target_date.weekday()]

    result = await Agent(_IDEATION_CFG, tools=[search_web]).run(
        messages=[HumanMessage(content="开始搜集今天的生活素材吧。")],
        prompt_vars={
            "recent_schedules": recent_schedules_text,
            "date": target_date.isoformat(),
            "weekday": weekday,
            "season": season,
        },
    )
    return _extract_text(result.content)


async def _run_writer(
    ideation_output: str,
    persona_core: str,
    weekly_plan: str,
    yesterday_journal: str,
    target_date: date,
    previous_output: str = "",
    critic_feedback: str = "",
) -> str:
    """Writer Agent: compose the daily journal/schedule."""
    weekday = _WEEKDAY_CN[target_date.weekday()]
    is_weekend = "周末！" if target_date.weekday() >= 5 else ""

    result = await Agent(_WRITER_CFG).run(
        messages=[HumanMessage(content="写今天的手帐")],
        prompt_vars={
            "persona_core": persona_core,
            "date": target_date.isoformat(),
            "weekday": weekday,
            "is_weekend": is_weekend,
            "weekly_plan": weekly_plan,
            "yesterday_journal": yesterday_journal,
            "ideation_output": ideation_output,
            "previous_output": previous_output,
            "critic_feedback": critic_feedback,
        },
    )
    return _extract_text(result.content)


async def _run_critic(
    schedule_text: str,
    recent_schedules_text: str,
    persona_name: str = "",
) -> str:
    """Critic Agent: review quality, return PASS or revision notes."""
    result = await Agent(_CRITIC_CFG).run(
        messages=[HumanMessage(content="审查今天的手帐质量")],
        prompt_vars={
            "persona_name": persona_name,
            "today_schedule": schedule_text,
            "recent_schedules": recent_schedules_text,
        },
    )
    return _extract_text(result.content)


# ---------------------------------------------------------------------------
# Monthly plan
# ---------------------------------------------------------------------------


async def generate_monthly_plan(
    persona_id: str, target_date: date | None = None
) -> str | None:
    """Generate a monthly plan -- broad life direction and mood for the month."""
    if target_date is None:
        target_date = date.today()

    month_start = target_date.replace(day=1)
    if month_start.month == 12:
        month_end = date(month_start.year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(month_start.year, month_start.month + 1, 1) - timedelta(days=1)

    period_start = month_start.isoformat()
    period_end = month_end.isoformat()

    async with get_session() as s:
        existing = await Q.find_plan_for_period(
            s, "monthly", period_start, period_end, persona_id
        )
    if existing:
        logger.info(
            "Monthly plan already exists for %s~%s, skip", period_start, period_end
        )
        return existing.content

    async with get_session() as s:
        prev_plan = await Q.find_latest_plan(s, "monthly", period_start, persona_id)
    prev_plan_text = prev_plan.content if prev_plan else "（这是第一个月计划）"

    season = _get_season(month_start.month)
    month_cn = f"{month_start.year}年{month_start.month}月"

    async with get_session() as s:
        persona = await Q.find_persona(s, persona_id)

    result = await Agent(_MONTHLY_CFG).run(
        messages=[HumanMessage(content="制定本月计划")],
        prompt_vars={
            "persona_name": persona.display_name if persona else persona_id,
            "persona_core": persona.persona_core if persona else "",
            "month": month_cn,
            "season": season,
            "previous_monthly_plan": prev_plan_text,
        },
    )
    content = _extract_text(result.content)

    if not content:
        logger.warning("LLM returned empty monthly plan for %s", month_cn)
        return None

    async with get_session() as s:
        await Q.upsert_schedule(
            s,
            AkaoSchedule(
                plan_type="monthly",
                period_start=period_start,
                period_end=period_end,
                persona_id=persona_id,
                content=content,
                model=_schedule_model(),
            ),
        )

    logger.info("Monthly plan generated: %s, %d chars", month_cn, len(content))
    return content


# ---------------------------------------------------------------------------
# Weekly plan
# ---------------------------------------------------------------------------


async def generate_weekly_plan(
    persona_id: str, target_date: date | None = None
) -> str | None:
    """Generate a weekly plan -- based on the monthly plan."""
    if target_date is None:
        target_date = date.today()

    week_start = target_date - timedelta(days=target_date.weekday())
    week_end = week_start + timedelta(days=6)
    period_start = week_start.isoformat()
    period_end = week_end.isoformat()

    async with get_session() as s:
        existing = await Q.find_plan_for_period(
            s, "weekly", period_start, period_end, persona_id
        )
    if existing:
        logger.info(
            "Weekly plan already exists for %s~%s, skip", period_start, period_end
        )
        return existing.content

    # Context: monthly plan
    month_start = target_date.replace(day=1).isoformat()
    if target_date.month == 12:
        month_end_d = date(target_date.year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end_d = date(target_date.year, target_date.month + 1, 1) - timedelta(
            days=1
        )

    async with get_session() as s:
        monthly = await Q.find_plan_for_period(
            s, "monthly", month_start, month_end_d.isoformat(), persona_id
        )
    monthly_text = monthly.content if monthly else "（暂无月计划）"

    async with get_session() as s:
        prev_plan = await Q.find_latest_plan(s, "weekly", period_start, persona_id)
    prev_plan_text = prev_plan.content if prev_plan else "（这是第一个周计划）"

    week_desc = (
        f"{period_start}（{_WEEKDAY_CN[week_start.weekday()]}）"
        f"~ {period_end}（{_WEEKDAY_CN[week_end.weekday()]}）"
    )

    async with get_session() as s:
        persona = await Q.find_persona(s, persona_id)

    result = await Agent(_WEEKLY_CFG).run(
        messages=[HumanMessage(content="制定本周计划")],
        prompt_vars={
            "persona_name": persona.display_name if persona else persona_id,
            "persona_core": persona.persona_core if persona else "",
            "week": week_desc,
            "monthly_plan": monthly_text,
            "previous_weekly_plan": prev_plan_text,
        },
    )
    content = _extract_text(result.content)

    if not content:
        logger.warning("LLM returned empty weekly plan for %s", week_desc)
        return None

    async with get_session() as s:
        await Q.upsert_schedule(
            s,
            AkaoSchedule(
                plan_type="weekly",
                period_start=period_start,
                period_end=period_end,
                persona_id=persona_id,
                content=content,
                model=_schedule_model(),
            ),
        )

    logger.info("Weekly plan generated: %s, %d chars", week_desc, len(content))
    return content


# ---------------------------------------------------------------------------
# Daily plan (Ideation -> Writer -> Critic pipeline)
# ---------------------------------------------------------------------------


async def generate_daily_plan(
    persona_id: str, target_date: date | None = None
) -> str | None:
    """Generate a daily plan via the three-Agent pipeline.

    Ideation brainstorms materials -> Writer composes -> Critic reviews.
    Writer rewrites up to 2 times if Critic rejects.
    """
    if target_date is None:
        target_date = date.today()

    date_str = target_date.isoformat()

    async with get_session() as s:
        existing = await Q.find_plan_for_period(
            s, "daily", date_str, date_str, persona_id
        )
    if existing:
        logger.info("Daily plan already exists for %s, skip", date_str)
        return existing.content

    # ---- Collect context ----
    async with get_session() as s:
        persona = await Q.find_persona(s, persona_id)
    persona_core = persona.persona_core if persona else ""
    persona_display_name = persona.display_name if persona else persona_id

    # Weekly plan
    week_start = target_date - timedelta(days=target_date.weekday())
    week_end = week_start + timedelta(days=6)
    async with get_session() as s:
        weekly = await Q.find_plan_for_period(
            s, "weekly", week_start.isoformat(), week_end.isoformat(), persona_id
        )
    weekly_text = weekly.content if weekly else "（暂无周计划）"

    # Yesterday's journal (from daily fragments)
    async with get_session() as s:
        recent_dailies = await Q.find_recent_fragments_by_grain(
            s, persona_id, "daily", limit=1
        )
    yesterday_journal = (
        recent_dailies[0].content if recent_dailies else "（昨天没有写日志）"
    )

    # Recent 3 days' schedules (shared by Ideation and Critic)
    recent = await _get_recent_daily_schedules(target_date, persona_id)
    recent_schedules_text = (
        "\n\n---\n\n".join(
            f"[{sched.period_start}]\n{sched.content}" for sched in recent
        )
        if recent
        else "（没有前几天的日程）"
    )

    # ---- Ideation Agent ----
    try:
        ideation_output = await _run_ideation(
            recent_schedules_text=recent_schedules_text,
            target_date=target_date,
        )
    except Exception as exc:
        logger.warning("Ideation agent failed, degrading: %s", exc, exc_info=True)
        ideation_output = ""

    # ---- Writer -> Critic loop ----
    feedback = ""
    previous_output = ""
    schedule_text = ""

    for attempt in range(3):
        schedule_text = await _run_writer(
            ideation_output=ideation_output,
            persona_core=persona_core,
            weekly_plan=weekly_text,
            yesterday_journal=yesterday_journal,
            target_date=target_date,
            previous_output=previous_output,
            critic_feedback=feedback,
        )

        critic_result = await _run_critic(
            schedule_text=schedule_text,
            recent_schedules_text=recent_schedules_text,
            persona_name=persona_display_name,
        )

        if "PASS" in critic_result:
            logger.info("Daily plan passed critic on attempt %d", attempt + 1)
            break

        logger.info(
            "Daily plan critic rejected (attempt %d): %s",
            attempt + 1,
            critic_result[:100],
        )
        previous_output = schedule_text
        feedback = critic_result

    if not schedule_text:
        logger.warning("Pipeline produced empty daily plan for %s", date_str)
        return None

    # ---- Persist ----
    async with get_session() as s:
        await Q.upsert_schedule(
            s,
            AkaoSchedule(
                plan_type="daily",
                period_start=date_str,
                period_end=date_str,
                persona_id=persona_id,
                content=schedule_text,
                model="offline-model",
            ),
        )

    logger.info("Daily plan generated for %s: %d chars", date_str, len(schedule_text))
    return schedule_text


# ---------------------------------------------------------------------------
# Schedule context builder (for injecting into chat system prompt)
# ---------------------------------------------------------------------------


async def build_schedule_context(persona_id: str | None = None) -> str:
    """Build the current daily schedule context for system prompt injection.

    Only injects today's journal content. Monthly/weekly plans are not
    injected into chat; they guide daily plan generation instead.

    Returns empty string if no daily plan exists.
    """
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")

    async with get_session() as s:
        daily = await Q.find_plan_for_period(s, "daily", today, today, persona_id or "")

    if not daily:
        return ""
    return daily.content
