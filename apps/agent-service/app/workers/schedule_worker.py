"""
赤尾日程生成 Worker — 多时间维度离线计划

三层生成器，各自独立节奏：
- 月计划（每月1号凌晨）：本月生活方向、兴趣倾向、季节氛围
- 周计划（每周日凌晨）：本周大致安排、松紧节奏
- 日计划（每天凌晨，紧跟日志之后）：状态、活动、精力

日计划的核心输入是昨天的个人日志（Journal），而非 per-chat 日记。
Journal + 周计划 + persona_core + web_search → 日计划。
日计划描述状态/活动，不描述具体话题（防止反馈循环）。
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.config.config import settings
from app.orm.crud import (
    get_daily_entries_for_date,
    get_latest_plan,
    get_plan_for_period,
    upsert_schedule,
)
from app.orm.models import AkaoSchedule

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 季节判定
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
    """日程生成使用的模型，复用 diary_model 配置"""
    return settings.diary_model


def _get_persona_core() -> str:
    """加载 persona_core prompt 作为人设上下文注入生成器"""
    try:
        return get_prompt("persona_core").compile()
    except Exception as e:
        logger.warning(f"Failed to load persona_core prompt: {e}")
        return ""


async def _gather_world_context(target_date: date) -> str:
    """搜索真实世界素材，为日计划提供延伸锚点

    搜索当季番剧、天气、热门话题等，让日计划能基于真实世界生长，
    而不是只在 persona 锚点里打转。
    """
    from app.agents.tools.search.web import search_web

    month = target_date.month
    season = _get_season(month)
    queries = [
        f"{target_date.year}年{month}月 新番动画 推荐",
        f"{season} 生活 日常 有趣的事",
    ]

    snippets: list[str] = []
    for q in queries:
        try:
            results = await search_web(query=q, num=3)
            for r in results[:3]:
                if r.get("snippet"):
                    snippets.append(r["snippet"])
        except Exception as e:
            logger.warning(f"World context search failed for '{q}': {e}")

    if not snippets:
        return ""

    return "以下是一些真实世界的近期信息（作为生活素材参考，自然融入而非罗列）：\n" + "\n".join(
        f"- {s}" for s in snippets[:6]
    )


# ==================== ArQ cron 入口 ====================


async def cron_generate_monthly_plan(ctx) -> None:
    """cron 入口：生成本月计划"""
    try:
        await generate_monthly_plan()
    except Exception as e:
        logger.error(f"Monthly plan generation failed: {e}", exc_info=True)


async def cron_generate_weekly_plan(ctx) -> None:
    """cron 入口：生成本周计划"""
    try:
        await generate_weekly_plan()
    except Exception as e:
        logger.error(f"Weekly plan generation failed: {e}", exc_info=True)


async def cron_generate_daily_plan(ctx) -> None:
    """cron 入口：生成今天的日计划"""
    try:
        await generate_daily_plan()
    except Exception as e:
        logger.error(f"Daily plan generation failed: {e}", exc_info=True)


# ==================== 月计划生成 ====================


async def generate_monthly_plan(target_date: date | None = None) -> str | None:
    """生成月度计划

    给出本月的生活方向和兴趣倾向，不要太具体。
    像一个真人月初时对这个月的模糊预期。

    Args:
        target_date: 目标月份的某一天，默认今天

    Returns:
        生成的月计划内容
    """
    if target_date is None:
        target_date = date.today()

    month_start = target_date.replace(day=1)
    # 月末
    if month_start.month == 12:
        month_end = date(month_start.year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(month_start.year, month_start.month + 1, 1) - timedelta(days=1)

    period_start = month_start.isoformat()
    period_end = month_end.isoformat()

    # 检查是否已有
    existing = await get_plan_for_period("monthly", period_start, period_end)
    if existing:
        logger.info(f"Monthly plan already exists for {period_start}~{period_end}, skip")
        return existing.content

    # 上下文：上月计划
    prev_plan = await get_latest_plan("monthly", period_start)
    prev_plan_text = prev_plan.content if prev_plan else "（这是第一个月计划）"

    season = _get_season(month_start.month)
    month_cn = f"{month_start.year}年{month_start.month}月"

    # 获取 Langfuse prompt（注入 persona_core）
    prompt_template = get_prompt("schedule_monthly")
    compiled = prompt_template.compile(
        persona_core=_get_persona_core(),
        month=month_cn,
        season=season,
        previous_monthly_plan=prev_plan_text,
    )

    # 调用 LLM
    model = await ModelBuilder.build_chat_model(_schedule_model())
    response = await model.ainvoke([{"role": "user", "content": compiled}])
    content = _extract_text(response.content)

    if not content:
        logger.warning(f"LLM returned empty monthly plan for {month_cn}")
        return None

    # 写入数据库
    await upsert_schedule(AkaoSchedule(
        plan_type="monthly",
        period_start=period_start,
        period_end=period_end,
        content=content,
        model=_schedule_model(),
    ))

    logger.info(f"Monthly plan generated: {month_cn}, {len(content)} chars")
    return content


# ==================== 周计划生成 ====================


async def generate_weekly_plan(target_date: date | None = None) -> str | None:
    """生成周计划

    基于月计划给出本周的大致安排。
    像一个真人周末时对下周的模糊规划。

    Args:
        target_date: 目标周的某一天，默认今天

    Returns:
        生成的周计划内容
    """
    if target_date is None:
        target_date = date.today()

    # 本周一和周日
    week_start = target_date - timedelta(days=target_date.weekday())
    week_end = week_start + timedelta(days=6)
    period_start = week_start.isoformat()
    period_end = week_end.isoformat()

    # 检查是否已有
    existing = await get_plan_for_period("weekly", period_start, period_end)
    if existing:
        logger.info(f"Weekly plan already exists for {period_start}~{period_end}, skip")
        return existing.content

    # 上下文
    # 1. 当前月计划
    month_start = target_date.replace(day=1).isoformat()
    if target_date.month == 12:
        month_end_d = date(target_date.year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end_d = date(target_date.year, target_date.month + 1, 1) - timedelta(days=1)
    monthly = await get_plan_for_period("monthly", month_start, month_end_d.isoformat())
    monthly_text = monthly.content if monthly else "（暂无月计划）"

    # 2. 上周计划
    prev_plan = await get_latest_plan("weekly", period_start)
    prev_plan_text = prev_plan.content if prev_plan else "（这是第一个周计划）"

    week_desc = f"{period_start}（{_WEEKDAY_CN[week_start.weekday()]}）~ {period_end}（{_WEEKDAY_CN[week_end.weekday()]}）"

    # 获取 Langfuse prompt（注入 persona_core）
    prompt_template = get_prompt("schedule_weekly")
    compiled = prompt_template.compile(
        persona_core=_get_persona_core(),
        week=week_desc,
        monthly_plan=monthly_text,
        previous_weekly_plan=prev_plan_text,
    )

    # 调用 LLM
    model = await ModelBuilder.build_chat_model(_schedule_model())
    response = await model.ainvoke([{"role": "user", "content": compiled}])
    content = _extract_text(response.content)

    if not content:
        logger.warning(f"LLM returned empty weekly plan for {week_desc}")
        return None

    # 写入数据库
    await upsert_schedule(AkaoSchedule(
        plan_type="weekly",
        period_start=period_start,
        period_end=period_end,
        content=content,
        model=_schedule_model(),
    ))

    logger.info(f"Weekly plan generated: {week_desc}, {len(content)} chars")
    return content


# ==================== 日计划生成 ====================


async def generate_daily_plan(target_date: date | None = None) -> str | None:
    """生成日计划

    核心输入:
    - 昨天的个人日志（Journal）— 真实经历提供延续感
    - 周计划 — 本周节奏方向
    - persona_core — 她的内核驱动力
    - web_search — 真实世界的新鲜素材

    输出描述状态/活动，不描述具体话题（防止反馈循环）。

    Args:
        target_date: 目标日期，默认今天

    Returns:
        生成的日计划内容
    """
    if target_date is None:
        target_date = date.today()

    date_str = target_date.isoformat()
    weekday = _WEEKDAY_CN[target_date.weekday()]
    is_weekend = target_date.weekday() >= 5

    # 检查是否已有
    existing = await get_plan_for_period("daily", date_str, date_str)
    if existing:
        logger.info(f"Daily plan already exists for {date_str}, skip")
        return existing.content

    # 1. 昨天的个人日志（替代原来从单个群取 diary 的 hack）
    from app.orm.crud import get_journal_for_date

    yesterday = (target_date - timedelta(days=1)).isoformat()
    yesterday_journal = await get_journal_for_date("daily", yesterday)
    journal_text = yesterday_journal.content if yesterday_journal else "（昨天没有日志）"

    # 2. 周计划
    week_start = target_date - timedelta(days=target_date.weekday())
    week_end = week_start + timedelta(days=6)
    weekly = await get_plan_for_period("weekly", week_start.isoformat(), week_end.isoformat())
    weekly_text = weekly.content if weekly else "（暂无周计划）"

    # 3. 搜索真实世界素材（当季番剧、热门话题等）
    world_context = await _gather_world_context(target_date)

    # 获取 Langfuse prompt
    prompt_template = get_prompt("schedule_daily")
    compiled = prompt_template.compile(
        persona_core=_get_persona_core(),
        date=date_str,
        weekday=weekday,
        is_weekend="周末！" if is_weekend else "",
        weekly_plan=weekly_text,
        yesterday_journal=journal_text,
        world_context=world_context,
    )

    # 调用 LLM
    model = await ModelBuilder.build_chat_model(_schedule_model())
    response = await model.ainvoke([{"role": "user", "content": compiled}])
    content = _extract_text(response.content)

    if not content:
        logger.warning(f"LLM returned empty daily plan for {date_str}")
        return None

    # 写入数据库（每天一条记录）
    await upsert_schedule(AkaoSchedule(
        plan_type="daily",
        period_start=date_str,
        period_end=date_str,
        content=content,
        model=_schedule_model(),
    ))

    logger.info(f"Daily plan generated for {date_str} ({weekday}): {len(content)} chars")
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


