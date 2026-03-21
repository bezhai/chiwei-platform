"""
赤尾个人日志合成 Worker — ArQ cron job

将 per-chat DiaryEntry（素材）+ 当天 Schedule 合成为赤尾级的统一个人日志。
日志不直接注入聊天上下文，而是喂给下一天的 Schedule 生成。

每经过一层传递，具体细节自然模糊一级：
  DiaryEntry (具体事件) → Journal (模糊话题) → Schedule (状态/活动)
  = 宣言里的 鲜明 → 模糊 → 印象 → 遗忘
"""

import logging
from datetime import date, timedelta, timezone

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.config.config import settings
from app.orm.crud import (
    get_all_diaries_for_date,
    get_journal_for_date,
    get_journals_in_range,
    get_plan_for_period,
    get_recent_journals,
    upsert_journal,
)

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


def _journal_model() -> str:
    """日志合成使用的模型，复用 diary_model 配置"""
    return settings.diary_model


def _get_persona_lite() -> str:
    """加载 persona_lite prompt 作为轻量人设（语气指导）"""
    try:
        return get_prompt("persona_lite").compile()
    except Exception as e:
        logger.warning(f"Failed to load persona_lite prompt: {e}")
        return ""


# ==================== ArQ cron 入口 ====================


async def cron_generate_journal(ctx) -> None:
    """cron 入口：合成昨天的个人日志"""
    yesterday = date.today() - timedelta(days=1)
    try:
        await generate_daily_journal(yesterday)
    except Exception as e:
        logger.error(f"Daily journal generation failed for {yesterday}: {e}", exc_info=True)


async def cron_generate_weekly_journal(ctx) -> None:
    """cron 入口：合成上周的周日志"""
    try:
        await generate_weekly_journal()
    except Exception as e:
        logger.error(f"Weekly journal generation failed: {e}", exc_info=True)


# ==================== Daily 日志合成 ====================


async def generate_daily_journal(target_date: date) -> str | None:
    """从所有 per-chat 日记 + 当天日程合成个人日志

    Args:
        target_date: 目标日期

    Returns:
        生成的日志内容，无素材时返回 None
    """
    date_str = target_date.isoformat()

    # 检查是否已有
    existing = await get_journal_for_date("daily", date_str)
    if existing:
        logger.info(f"Daily journal already exists for {date_str}, skip")
        return existing.content

    # 1. 收集素材：所有聊天的日记
    diaries = await get_all_diaries_for_date(date_str)
    if not diaries:
        logger.info(f"No diaries for {date_str}, skip journal")
        return None

    # 格式化素材（标注来源类型但不暴露具体 chat_id）
    diary_parts: list[str] = []
    for i, diary in enumerate(diaries, 1):
        diary_parts.append(f"--- 素材 {i} ---\n{diary.content}")
    diaries_text = "\n\n".join(diary_parts)

    # 2. 当天日程（她计划做什么）
    daily_schedule = await get_plan_for_period("daily", date_str, date_str)
    schedule_text = daily_schedule.content if daily_schedule else "（今天没有特别的计划）"

    # 3. 昨天的日志（连续性）
    yesterday_str = (target_date - timedelta(days=1)).isoformat()
    yesterday_journal = await get_journal_for_date("daily", yesterday_str)
    yesterday_text = yesterday_journal.content if yesterday_journal else "（没有昨天的日志）"

    # 4. 获取 Langfuse prompt 并编译
    persona_lite = _get_persona_lite()
    prompt_template = get_prompt("journal_generation")
    compiled = prompt_template.compile(
        persona_lite=persona_lite,
        date=date_str,
        chat_diaries=diaries_text,
        daily_schedule=schedule_text,
        yesterday_journal=yesterday_text,
    )

    # 5. 调用 LLM
    model = await ModelBuilder.build_chat_model(_journal_model())
    response = await model.ainvoke(
        [{"role": "user", "content": compiled}],
    )
    content = _extract_text(response.content)

    if not content:
        logger.warning(f"LLM returned empty journal for {date_str}")
        return None

    # 6. 写入数据库
    await upsert_journal(
        journal_type="daily",
        journal_date=date_str,
        period_end=date_str,
        content=content,
        source_chat_count=len(diaries),
        model=_journal_model(),
    )

    logger.info(
        f"Daily journal generated for {date_str}: "
        f"{len(diaries)} diaries → {len(content)} chars"
    )
    return content


# ==================== Weekly 周日志合成 ====================


async def generate_weekly_journal(target_monday: date | None = None) -> str | None:
    """从 7 篇 daily 日志合成周日志

    Args:
        target_monday: 目标周的周一日期，默认为上周一

    Returns:
        生成的周日志内容，无素材时返回 None
    """
    if target_monday is None:
        today = date.today()
        target_monday = today - timedelta(days=today.weekday() + 7)

    week_start = target_monday.isoformat()
    week_end = (target_monday + timedelta(days=6)).isoformat()

    # 检查是否已有
    existing = await get_journal_for_date("weekly", week_start)
    if existing:
        logger.info(f"Weekly journal already exists for {week_start}~{week_end}, skip")
        return existing.content

    # 1. 收集素材：本周的 daily 日志
    journals = await get_journals_in_range("daily", week_start, week_end)
    if not journals:
        logger.info(f"No daily journals for {week_start}~{week_end}, skip")
        return None

    journals_text = "\n\n".join(
        f"--- {j.journal_date} ---\n{j.content}" for j in journals
    )

    # 2. 上周的周日志（连续性）
    prev_journals = await get_recent_journals("weekly", week_start, limit=1)
    prev_text = prev_journals[0].content if prev_journals else "（没有上周的周日志）"

    # 3. 获取 Langfuse prompt 并编译
    persona_lite = _get_persona_lite()
    prompt_template = get_prompt("journal_weekly")
    compiled = prompt_template.compile(
        persona_lite=persona_lite,
        week_start=week_start,
        week_end=week_end,
        daily_journals=journals_text,
        previous_weekly_journal=prev_text,
    )

    # 4. 调用 LLM
    model = await ModelBuilder.build_chat_model(_journal_model())
    response = await model.ainvoke(
        [{"role": "user", "content": compiled}],
    )
    content = _extract_text(response.content)

    if not content:
        logger.warning(f"LLM returned empty weekly journal for {week_start}~{week_end}")
        return None

    # 5. 写入数据库
    await upsert_journal(
        journal_type="weekly",
        journal_date=week_start,
        period_end=week_end,
        content=content,
        source_chat_count=len(journals),
        model=_journal_model(),
    )

    logger.info(
        f"Weekly journal generated for {week_start}~{week_end}: "
        f"{len(journals)} daily journals → {len(content)} chars"
    )
    return content


# ==================== 辅助函数 ====================


def _extract_text(content) -> str:
    """从 LLM 响应中提取文本"""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return content or ""
