"""
赤尾个人日志生成 Worker

Journal 是 DiaryEntry 和 Schedule 之间的桥梁：
- DiaryEntry: per-chat 的具体事件和话题
- Journal: 赤尾级的模糊化感受（"和朋友聊了有趣的新番" 而非 "陈儒推荐了《夜樱家》"）
- Schedule: 从 Journal 的感受出发生成今日状态

夜间管线时序：
  03:00  diary_worker → DiaryEntry + 印象
  04:00  journal_worker → Journal daily（本文件）
  04:45  journal_worker → Journal weekly（每周一）
  05:00  schedule_worker → Schedule daily
"""

import logging
from datetime import date, timedelta

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.config.config import settings
from app.orm.crud import (
    get_all_diaries_for_date,
    get_journal,
    get_plan_for_period,
    get_recent_journals,
    upsert_journal,
)

logger = logging.getLogger(__name__)


def _journal_model() -> str:
    return settings.diary_model


async def _get_persona_lite_for_bot(bot_name: str) -> str:
    """从 bot_persona 表加载 persona_lite"""
    from app.orm.crud import get_bot_persona
    try:
        persona = await get_bot_persona(bot_name)
        return persona.persona_lite if persona else ""
    except Exception as e:
        logger.warning(f"[{bot_name}] Failed to load persona_lite: {e}")
        return ""


async def _get_recent_journals_text(target_date: date, limit: int = 3) -> str:
    """获取前 N 天的 daily journal 内容，用于避免重复意象"""
    journals = await get_recent_journals("daily", target_date.isoformat(), limit=limit)
    if not journals:
        return "（前几天没有日志）"
    return "\n\n".join(
        f"--- {j.journal_date} ---\n{j.content}" for j in journals
    )


def _extract_text(content) -> str:
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return content or ""


# ==================== ArQ cron 入口 ====================


async def cron_generate_daily_journal(ctx) -> None:
    """cron 入口：为每个 persona bot 生成昨天的 daily journal"""
    from app.orm.crud import get_all_persona_bot_names

    yesterday = date.today() - timedelta(days=1)
    for bot_name in await get_all_persona_bot_names():
        try:
            await generate_daily_journal(yesterday, bot_name=bot_name)
        except Exception as e:
            logger.error(f"[{bot_name}] Daily journal generation failed: {e}", exc_info=True)


async def cron_generate_weekly_journal(ctx) -> None:
    """cron 入口：为每个 persona bot 生成上周的 weekly journal"""
    from app.orm.crud import get_all_persona_bot_names

    last_monday = date.today() - timedelta(days=7)
    for bot_name in await get_all_persona_bot_names():
        try:
            await generate_weekly_journal(last_monday, bot_name=bot_name)
        except Exception as e:
            logger.error(f"[{bot_name}] Weekly journal generation failed: {e}", exc_info=True)


# ==================== Daily Journal 生成 ====================


async def generate_daily_journal(
    target_date: date, bot_name: str = "chiwei"
) -> str | None:
    """生成赤尾的每日个人日志

    从当天所有群/私聊的 DiaryEntry 合成，模糊化话题只保留感受和氛围。

    Args:
        target_date: 日志对应的日期（通常是昨天）
        bot_name: bot 名称，用于加载对应人设

    Returns:
        生成的日志内容，或 None（无日记/已存在）
    """
    date_str = target_date.isoformat()

    # 检查是否已有
    existing = await get_journal("daily", date_str)
    if existing:
        logger.info(f"Daily journal already exists for {date_str}, skip")
        return existing.content

    # 收集当天所有 DiaryEntry
    diaries = await get_all_diaries_for_date(date_str)
    if not diaries:
        logger.info(f"No diaries for {date_str}, skip journal generation")
        return None

    # 拼接日记内容
    chat_diaries = "\n\n".join(
        f"--- 群/私聊 {i+1} ---\n{d.content}" for i, d in enumerate(diaries)
    )

    # 加载上下文
    daily_schedule = await get_plan_for_period("daily", date_str, date_str)
    schedule_text = daily_schedule.content if daily_schedule else "（今天没有写手帐）"

    recent_journals = await _get_recent_journals_text(target_date)

    # 编译 prompt
    prompt_template = get_prompt("journal_generation")
    compiled = prompt_template.compile(
        persona_lite=await _get_persona_lite_for_bot(bot_name),
        date=date_str,
        chat_diaries=chat_diaries,
        daily_schedule=schedule_text,
        recent_journals=recent_journals,
    )

    # 调用 LLM
    model = await ModelBuilder.build_chat_model(_journal_model())
    response = await model.ainvoke([{"role": "user", "content": compiled}])
    content = _extract_text(response.content)

    if not content:
        logger.warning(f"LLM returned empty journal for {date_str}")
        return None

    # 写入数据库
    await upsert_journal(
        "daily", date_str, content, _journal_model(),
        period_end=date_str, source_chat_count=len(diaries),
    )

    logger.info(f"Daily journal generated for {date_str}: {len(content)} chars")
    return content


# ==================== Weekly Journal 生成 ====================


async def generate_weekly_journal(
    monday_date: date, bot_name: str = "chiwei"
) -> str | None:
    """生成赤尾的每周日志

    从 7 篇 daily journal 合成，进一步模糊化。

    Args:
        monday_date: 目标周的周一日期
        bot_name: bot 名称，用于加载对应人设

    Returns:
        生成的周日志内容，或 None
    """
    week_start = monday_date.isoformat()
    week_end = (monday_date + timedelta(days=6)).isoformat()

    # 检查是否已有
    existing = await get_journal("weekly", week_start)
    if existing:
        logger.info(f"Weekly journal already exists for week {week_start}, skip")
        return existing.content

    # 收集本周的 daily journals
    daily_journals = []
    for i in range(7):
        d = monday_date + timedelta(days=i)
        journal = await get_journal("daily", d.isoformat())
        if journal:
            daily_journals.append(f"--- {d.isoformat()} ---\n{journal.content}")

    if not daily_journals:
        logger.info(f"No daily journals for week {week_start}, skip")
        return None

    journals_text = "\n\n".join(daily_journals)

    # 编译 prompt
    prompt_template = get_prompt("journal_weekly")
    compiled = prompt_template.compile(
        persona_lite=await _get_persona_lite_for_bot(bot_name),
        week_start=week_start,
        week_end=week_end,
        daily_journals=journals_text,
    )

    # 调用 LLM
    model = await ModelBuilder.build_chat_model(_journal_model())
    response = await model.ainvoke([{"role": "user", "content": compiled}])
    content = _extract_text(response.content)

    if not content:
        logger.warning(f"LLM returned empty weekly journal for week {week_start}")
        return None

    # 写入数据库
    await upsert_journal(
        "weekly", week_start, content, _journal_model(),
        period_end=week_end, source_chat_count=len(daily_journals),
    )

    logger.info(f"Weekly journal generated for week {week_start}: {len(content)} chars")
    return content
