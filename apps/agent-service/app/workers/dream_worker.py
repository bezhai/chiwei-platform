"""做梦管线 — 赤尾睡前回想今天

daily dream: 当天 conversation + glimpse 碎片 → 第一人称回顾
weekly dream: 最近 7 个 daily 碎片 → 一周回顾

替代 v2 的 diary_worker + journal_worker。
遗忘在此自然发生：十几条碎片压缩成一篇回顾。
"""

import logging
from datetime import date, datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from app.agents.infra.llm_service import LLMService
from app.config.config import settings
from app.orm.crud import get_all_persona_ids, get_bot_persona
from app.orm.memory_crud import (
    create_fragment,
    get_fragments_in_date_range,
    get_recent_fragments_by_grain,
)
from app.orm.memory_models import ExperienceFragment
from app.workers.error_handling import cron_error_handler

logger = logging.getLogger(__name__)
CST = timezone(timedelta(hours=8))


@cron_error_handler()
async def cron_generate_dreams(ctx) -> None:
    """cron 入口：为每个 persona 生成昨天的 daily dream"""
    yesterday = date.today() - timedelta(days=1)
    persona_ids = await get_all_persona_ids()
    for persona_id in persona_ids:
        try:
            await generate_daily_dream(persona_id, yesterday)
        except Exception as e:
            logger.error(f"[{persona_id}] Daily dream failed: {e}", exc_info=True)


@cron_error_handler()
async def cron_generate_weekly_dreams(ctx) -> None:
    """cron 入口：每周一为每个 persona 生成 weekly dream"""
    today = date.today()
    persona_ids = await get_all_persona_ids()
    for persona_id in persona_ids:
        try:
            await generate_weekly_dream(persona_id, today)
        except Exception as e:
            logger.error(f"[{persona_id}] Weekly dream failed: {e}", exc_info=True)


async def generate_daily_dream(persona_id: str, target_date: date | None = None) -> ExperienceFragment | None:
    """生成 daily 碎片"""
    if target_date is None:
        target_date = date.today() - timedelta(days=1)

    day_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=CST)
    day_end = day_start + timedelta(days=1)

    today_frags = await get_fragments_in_date_range(
        persona_id, target_date, target_date, grains=["conversation", "glimpse"]
    )
    if not today_frags:
        logger.info(f"[{persona_id}] No fragments for {target_date}, skip daily dream")
        return None

    persona_obj = await get_bot_persona(persona_id)
    recent_dailies = await get_recent_fragments_by_grain(persona_id, "daily", limit=3)

    persona_name = persona_obj.display_name if persona_obj else persona_id
    persona_lite = persona_obj.persona_lite if persona_obj else ""
    today_text = "\n\n---\n\n".join(f.content for f in today_frags)
    recent_text = (
        "\n\n---\n\n".join(f.content for f in reversed(recent_dailies))
        if recent_dailies
        else "（前几天没有做梦）"
    )

    result = await LLMService.run(
        prompt_id="dream_daily",
        prompt_vars={
            "persona_name": persona_name,
            "persona_lite": persona_lite,
            "date": target_date.isoformat(),
            "today_fragments": today_text,
            "recent_dreams": recent_text,
        },
        messages=[HumanMessage(content="回忆今天发生的事")],
        model_id=settings.diary_model,
        trace_name="dream-daily",
    )
    content = _extract_text(result.content)

    if not content:
        logger.warning(f"[{persona_id}] Daily dream LLM returned empty for {target_date}")
        return None

    fragment = ExperienceFragment(
        persona_id=persona_id,
        grain="daily",
        content=content,
        time_start=int(day_start.timestamp() * 1000),
        time_end=int(day_end.timestamp() * 1000),
        model=settings.diary_model,
    )
    saved = await create_fragment(fragment)
    logger.info(f"[{persona_id}] Daily dream created: id={saved.id}, date={target_date}, len={len(content)}")
    return saved


async def generate_weekly_dream(persona_id: str, target_date: date | None = None) -> ExperienceFragment | None:
    """生成 weekly 碎片 from 最近 7 个 daily 碎片"""
    if target_date is None:
        target_date = date.today()

    dailies = await get_recent_fragments_by_grain(persona_id, "daily", limit=7)
    if not dailies:
        logger.info(f"[{persona_id}] No daily fragments for weekly dream, skip")
        return None

    persona_obj = await get_bot_persona(persona_id)
    persona_name = persona_obj.display_name if persona_obj else persona_id
    persona_lite = persona_obj.persona_lite if persona_obj else ""
    dailies_text = "\n\n---\n\n".join(f.content for f in reversed(dailies))

    result = await LLMService.run(
        prompt_id="dream_weekly",
        prompt_vars={
            "persona_name": persona_name,
            "persona_lite": persona_lite,
            "dailies": dailies_text,
        },
        messages=[HumanMessage(content="回顾这一周")],
        model_id=settings.diary_model,
        trace_name="dream-weekly",
    )
    content = _extract_text(result.content)

    if not content:
        logger.warning(f"[{persona_id}] Weekly dream LLM returned empty")
        return None

    week_end = datetime(target_date.year, target_date.month, target_date.day, tzinfo=CST)
    week_start = week_end - timedelta(days=7)

    fragment = ExperienceFragment(
        persona_id=persona_id,
        grain="weekly",
        content=content,
        time_start=int(week_start.timestamp() * 1000),
        time_end=int(week_end.timestamp() * 1000),
        model=settings.diary_model,
    )
    saved = await create_fragment(fragment)
    logger.info(f"[{persona_id}] Weekly dream created: id={saved.id}, len={len(content)}")
    return saved


def _extract_text(content) -> str:
    """提取 LLM 响应中的文本内容（兼容 Gemini list 格式）"""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return (content or "").strip()
