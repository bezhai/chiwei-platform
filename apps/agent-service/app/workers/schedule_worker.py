"""
赤尾日程生成 Worker — 多时间维度离线计划

三层生成器，各自独立节奏：
- 月计划（每月1号凌晨）：本月生活方向、兴趣倾向、季节氛围
- 周计划（每周日凌晨）：本周大致安排、松紧节奏
- 日计划（每天凌晨，紧跟日记之后）：逐时段的活动、心情、精力

每层继承上层输出，自上而下细化。方向性指导而非刻板时间表。
日记系统回馈日计划（昨天的日记影响今天的安排）。
"""

import json
import logging
import random
from datetime import date, datetime, timedelta, timezone

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.config.config import settings
from app.orm.crud import (
    get_daily_entries_for_date,
    get_latest_plan,
    get_plan_for_period,
    get_recent_diaries,
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


# 生活维度池（基于宣言和 persona_core）
_WORLD_CONTEXT_DIMENSIONS = [
    {
        "dim": "anime",
        "label": "二次元",
        "queries": [
            "{year}年{month}月 新番动画 推荐",
            "最近热门 动画 讨论",
        ],
    },
    {
        "dim": "music",
        "label": "音乐",
        "queries": [
            "最新 日语歌 推荐 {year}",
            "独立音乐 最近 好听的歌",
        ],
    },
    {
        "dim": "photography",
        "label": "摄影",
        "queries": [
            "胶片摄影 {season} 拍摄 灵感",
            "街头摄影 构图 技巧",
        ],
    },
    {
        "dim": "food",
        "label": "美食",
        "queries": [
            "简单甜品 食谱 新手",
            "新开的 咖啡店 甜品店 推荐",
        ],
    },
    {
        "dim": "knowledge",
        "label": "冷知识",
        "queries": [
            "有趣的冷知识 最近",
            "植物 {season} 花期",
        ],
    },
    {
        "dim": "weather",
        "label": "天气",
        "queries": [
            "北京 今天 天气",
        ],
    },
    {
        "dim": "trending",
        "label": "热点",
        "queries": [
            "今天 有趣的事 互联网",
            "最近 社交媒体 热门话题",
        ],
    },
    {
        "dim": "city",
        "label": "城市探索",
        "queries": [
            "周末 好去处 散步 咖啡",
            "有趣的 文具店 杂货铺",
        ],
    },
]


def _select_dimensions(target_date: date) -> list[dict]:
    """从维度池中选取 4-6 个维度

    - 天气必选
    - 其余随机选 3-5 个
    """
    weather = [d for d in _WORLD_CONTEXT_DIMENSIONS if d["dim"] == "weather"]
    others = [d for d in _WORLD_CONTEXT_DIMENSIONS if d["dim"] != "weather"]

    # 用日期做种子，同一天多次调用结果一致
    rng = random.Random(target_date.isoformat())
    count = rng.randint(3, 5)
    selected = rng.sample(others, min(count, len(others)))

    return weather + selected


def _build_active_dimensions_text(dims: list[dict]) -> str:
    """构建 active_dimensions 提示文本"""
    labels = [d["label"] for d in dims if d["dim"] != "weather"]
    return "今天可能涉及：" + "、".join(labels)


async def _gather_world_context(target_date: date) -> tuple[str, str]:
    """搜索真实世界素材，返回 (world_context, active_dimensions_text)

    从选中的维度中各取一个 query 搜索，收集 snippets。
    """
    from app.agents.tools.search.web import search_web

    dims = _select_dimensions(target_date)
    active_dims_text = _build_active_dimensions_text(dims)

    month = target_date.month
    year = target_date.year
    season = _get_season(month)

    snippets: list[str] = []
    for dim in dims:
        # 每个维度随机选一个 query
        rng = random.Random(f"{target_date.isoformat()}-{dim['dim']}")
        query_template = rng.choice(dim["queries"])
        query = query_template.format(year=year, month=month, season=season)

        try:
            results = await search_web(query=query, num=3)
            for r in results[:2]:
                if r.get("snippet"):
                    snippets.append(r["snippet"])
        except Exception as e:
            logger.warning(f"World context search failed for '{query}': {e}")

    if not snippets:
        return "", active_dims_text

    world_text = "以下是一些真实世界的近期信息（作为生活素材参考，自然融入而非罗列）：\n" + "\n".join(
        f"- {s}" for s in snippets[:8]
    )
    return world_text, active_dims_text


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
    """生成日计划（手帐式 markdown）

    基于月计划 + 周计划 + 昨天日记，为今天写一篇私人手帐。
    每天一条记录，内容是自然的 markdown 文本。

    Args:
        target_date: 目标日期，默认今天

    Returns:
        生成的手帐内容
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

    # 上下文
    # 1. 月计划
    month_start = target_date.replace(day=1).isoformat()
    if target_date.month == 12:
        month_end_d = date(target_date.year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end_d = date(target_date.year, target_date.month + 1, 1) - timedelta(days=1)
    monthly = await get_plan_for_period("monthly", month_start, month_end_d.isoformat())
    monthly_text = monthly.content if monthly else "（暂无月计划）"

    # 2. 周计划
    week_start = target_date - timedelta(days=target_date.weekday())
    week_end = week_start + timedelta(days=6)
    weekly = await get_plan_for_period("weekly", week_start.isoformat(), week_end.isoformat())
    weekly_text = weekly.content if weekly else "（暂无周计划）"

    # 3. 最近日记（从活跃群取）
    recent_diaries = []
    from app.orm.crud import get_active_diary_chat_ids
    active_chats = await get_active_diary_chat_ids(min_replies=3, days=7)
    for cid in active_chats[:1]:
        recent_diaries = await get_recent_diaries(cid, date_str, limit=2)
        if recent_diaries:
            break

    if recent_diaries:
        diary_parts = []
        for d in reversed(recent_diaries):
            diary_parts.append(f"--- {d.diary_date} ---\n{d.content}")
        diary_text = "\n\n".join(diary_parts)
    else:
        diary_text = "（暂无近期日记）"

    # 4. 昨天的手帐
    yesterday = (target_date - timedelta(days=1)).isoformat()
    yesterday_plan = await get_plan_for_period("daily", yesterday, yesterday)
    yesterday_text = yesterday_plan.content if yesterday_plan else "（暂无昨天的手帐）"

    # 5. 搜索真实世界素材（当季番剧、热门话题等）
    world_context = await _gather_world_context(target_date)

    # 获取 Langfuse prompt（注入 persona_core + world_context）
    prompt_template = get_prompt("schedule_daily")
    compiled = prompt_template.compile(
        persona_core=_get_persona_core(),
        date=date_str,
        weekday=weekday,
        is_weekend="周末！" if is_weekend else "",
        monthly_plan=monthly_text,
        weekly_plan=weekly_text,
        recent_diary=diary_text,
        yesterday_plan=yesterday_text,
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


