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

    # 获取 Langfuse prompt
    prompt_template = get_prompt("schedule_monthly")
    compiled = prompt_template.compile(
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

    # 获取 Langfuse prompt
    prompt_template = get_prompt("schedule_weekly")
    compiled = prompt_template.compile(
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


async def generate_daily_plan(target_date: date | None = None) -> list[dict] | None:
    """生成日计划

    基于月计划 + 周计划 + 昨天日记，为今天生成逐时段的安排。
    输出 JSON 数组，每个元素是一个时段。

    Args:
        target_date: 目标日期，默认今天

    Returns:
        生成的时段列表
    """
    if target_date is None:
        target_date = date.today()

    date_str = target_date.isoformat()
    weekday = _WEEKDAY_CN[target_date.weekday()]
    is_weekend = target_date.weekday() >= 5

    # 检查是否已有
    existing = await get_daily_entries_for_date(date_str)
    if existing:
        logger.info(f"Daily plan already exists for {date_str}, skip ({len(existing)} entries)")
        return None

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

    # 3. 昨天的日记（最近 2 篇，作为生活连续性参考）
    recent_diaries = await get_recent_diaries(
        # 日记是按 chat_id 存的，这里取所有群的不太合适
        # 但日程是全局的，取任一活跃群的日记作为参考即可
        # TODO: 后续可改为取多群日记的摘要
        chat_id="__global__",  # 占位，实际逻辑见下方 fallback
        before_date=date_str,
        limit=2,
    )
    # fallback: 如果 __global__ 无日记，尝试从任意活跃群取
    if not recent_diaries:
        from app.orm.crud import get_active_diary_chat_ids
        active_chats = await get_active_diary_chat_ids(min_replies=3, days=7)
        for cid in active_chats[:1]:
            recent_diaries = await get_recent_diaries(cid, date_str, limit=2)
            if recent_diaries:
                break

    diary_text = ""
    if recent_diaries:
        diary_parts = []
        for d in reversed(recent_diaries):
            diary_parts.append(f"--- {d.diary_date} ---\n{d.content}")
        diary_text = "\n\n".join(diary_parts)
    else:
        diary_text = "（暂无近期日记）"

    # 4. 昨天的日计划（参考连续性）
    yesterday = (target_date - timedelta(days=1)).isoformat()
    yesterday_entries = await get_daily_entries_for_date(yesterday)
    if yesterday_entries:
        yesterday_plan = "\n".join(
            f"{e.time_start}-{e.time_end}: {e.content}" for e in yesterday_entries
        )
    else:
        yesterday_plan = "（暂无昨天的日计划）"

    # 获取 Langfuse prompt
    prompt_template = get_prompt("schedule_daily")
    compiled = prompt_template.compile(
        date=date_str,
        weekday=weekday,
        is_weekend="是" if is_weekend else "否",
        monthly_plan=monthly_text,
        weekly_plan=weekly_text,
        recent_diary=diary_text,
        yesterday_plan=yesterday_plan,
    )

    # 调用 LLM
    model = await ModelBuilder.build_chat_model(_schedule_model())
    response = await model.ainvoke([{"role": "user", "content": compiled}])
    raw = _extract_text(response.content)

    if not raw:
        logger.warning(f"LLM returned empty daily plan for {date_str}")
        return None

    # 解析 JSON 数组
    entries = _parse_daily_entries(raw)
    if not entries:
        logger.warning(f"Failed to parse daily plan for {date_str}: {raw[:200]}")
        return None

    # 写入数据库
    for entry_data in entries:
        await upsert_schedule(AkaoSchedule(
            plan_type="daily",
            period_start=date_str,
            period_end=date_str,
            time_start=entry_data["time_start"],
            time_end=entry_data["time_end"],
            content=entry_data["content"],
            mood=entry_data.get("mood"),
            energy_level=entry_data.get("energy_level"),
            response_style_hint=entry_data.get("response_style_hint"),
            model=_schedule_model(),
        ))

    logger.info(f"Daily plan generated for {date_str} ({weekday}): {len(entries)} time blocks")
    return entries


# ==================== 辅助函数 ====================


def _extract_text(content) -> str:
    """从 LLM 响应中提取文本"""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return content or ""


def _parse_daily_entries(raw: str) -> list[dict] | None:
    """从 LLM 输出中解析日计划 JSON 数组

    期望格式:
    [
        {
            "time_start": "07:00",
            "time_end": "08:30",
            "content": "赖床中，闹钟响了但不想起来",
            "mood": "困",
            "energy_level": 2,
            "response_style_hint": "迷迷糊糊，说话断断续续"
        },
        ...
    ]
    """
    raw = raw.strip()

    # 去掉 markdown 代码块包裹
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Daily plan JSON parse failed, attempting to extract JSON array")
        # 尝试从文本中提取 JSON 数组
        start = raw.find("[")
        end = raw.rfind("]")
        if start >= 0 and end > start:
            try:
                entries = json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return None
        else:
            return None

    if not isinstance(entries, list):
        return None

    # 验证必需字段
    valid = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not entry.get("time_start") or not entry.get("time_end") or not entry.get("content"):
            continue
        valid.append({
            "time_start": entry["time_start"],
            "time_end": entry["time_end"],
            "content": entry["content"],
            "mood": entry.get("mood"),
            "energy_level": entry.get("energy_level"),
            "response_style_hint": entry.get("response_style_hint"),
        })

    return valid if valid else None
