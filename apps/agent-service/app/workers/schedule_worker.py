"""
赤尾日程生成 Worker — 多时间维度离线计划

三层生成器，各自独立节奏：
- 月计划（每月1号凌晨）：本月生活方向、兴趣倾向、季节氛围
- 周计划（每周日凌晨）：本周大致安排、松紧节奏
- 日计划（每天凌晨，紧跟日记之后）：逐时段的活动、心情、精力

每层继承上层输出，自上而下细化。方向性指导而非刻板时间表。
日记系统回馈日计划（昨天的日记影响今天的安排）。
"""

import logging
from datetime import date, datetime, timedelta, timezone

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.config.config import settings
from app.orm.crud import (
    get_journal,
    get_latest_plan,
    get_plan_for_period,
    list_schedules,
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


async def _get_persona_core_for_bot(persona_id: str) -> str:
    """从 bot_persona 表加载 persona_core"""
    from app.orm.crud import get_bot_persona
    try:
        persona = await get_bot_persona(persona_id)
        return persona.persona_core if persona else ""
    except Exception as e:
        logger.warning(f"[{persona_id}] Failed to load persona_core: {e}")
        return ""


async def _get_recent_daily_schedules(before_date: date, persona_id: str = "akao", count: int = 3) -> list[AkaoSchedule]:
    """获取前 N 天的 daily schedule（供 Ideation 和 Critic 去重）"""
    results = await list_schedules(plan_type="daily", persona_id=persona_id, active_only=True, limit=count + 5)
    return [
        s for s in results
        if s.period_start < before_date.isoformat()
    ][:count]


async def _run_ideation(
    recent_schedules_text: str,
    target_date: date,
) -> str:
    """运行 Ideation Agent：广撒网搜集外部世界素材（不带人设，避免搜索被兴趣带偏）"""
    from langchain.agents import create_agent
    from langchain.messages import HumanMessage
    from langfuse.langchain import CallbackHandler

    from app.agents.core.config import AgentRegistry
    from app.agents.tools.search.web import search_web

    config = AgentRegistry.get("schedule-ideation")
    prompt_template = get_prompt(config.prompt_id)

    season = _get_season(target_date.month)
    weekday = _WEEKDAY_CN[target_date.weekday()]

    compiled = prompt_template.compile(
        recent_schedules=recent_schedules_text,
        date=target_date.isoformat(),
        weekday=weekday,
        season=season,
    )

    model = await ModelBuilder.build_chat_model(config.model_id)
    agent = create_agent(model, [search_web], system_prompt=compiled)

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="开始搜集今天的生活素材吧。")]},
        config={
            "callbacks": [CallbackHandler()],
            "run_name": config.trace_name,
            "recursion_limit": 42,
        },
    )

    last_msg = result["messages"][-1]
    return _extract_text(last_msg.content)


async def _run_writer(
    ideation_output: str,
    persona_core: str,
    weekly_plan: str,
    yesterday_journal: str,
    target_date: date,
    previous_output: str = "",
    critic_feedback: str = "",
) -> str:
    """运行 Writer Agent：基于素材写手帐"""
    from app.agents.core.config import AgentRegistry

    config = AgentRegistry.get("schedule-writer")
    prompt_template = get_prompt(config.prompt_id)

    weekday = _WEEKDAY_CN[target_date.weekday()]
    is_weekend = "周末！" if target_date.weekday() >= 5 else ""

    compiled = prompt_template.compile(
        persona_core=persona_core,
        date=target_date.isoformat(),
        weekday=weekday,
        is_weekend=is_weekend,
        weekly_plan=weekly_plan,
        yesterday_journal=yesterday_journal,
        ideation_output=ideation_output,
        previous_output=previous_output,
        critic_feedback=critic_feedback,
    )

    model = await ModelBuilder.build_chat_model(config.model_id)
    response = await model.ainvoke([{"role": "user", "content": compiled}])
    return _extract_text(response.content)


async def _run_critic(
    schedule_text: str,
    recent_schedules_text: str,
) -> str:
    """运行 Critic Agent：审查质量并返回 PASS 或修改建议"""
    from app.agents.core.config import AgentRegistry

    config = AgentRegistry.get("schedule-critic")
    prompt_template = get_prompt(config.prompt_id)

    compiled = prompt_template.compile(
        today_schedule=schedule_text,
        recent_schedules=recent_schedules_text,
    )

    model = await ModelBuilder.build_chat_model(config.model_id)
    response = await model.ainvoke([{"role": "user", "content": compiled}])
    return _extract_text(response.content)


# ==================== ArQ cron 入口 ====================


async def cron_generate_monthly_plan(ctx) -> None:
    """cron 入口：为每个 persona bot 生成本月计划"""
    from app.orm.crud import get_all_persona_ids

    for persona_id in await get_all_persona_ids():
        try:
            await generate_monthly_plan(persona_id=persona_id)
        except Exception as e:
            logger.error(f"[{persona_id}] Monthly plan generation failed: {e}", exc_info=True)


async def cron_generate_weekly_plan(ctx) -> None:
    """cron 入口：周日 23:00 触发，为每个 persona bot 生成下周计划"""
    from app.orm.crud import get_all_persona_ids

    tomorrow = date.today() + timedelta(days=1)
    for persona_id in await get_all_persona_ids():
        try:
            await generate_weekly_plan(target_date=tomorrow, persona_id=persona_id)
        except Exception as e:
            logger.error(f"[{persona_id}] Weekly plan generation failed: {e}", exc_info=True)


async def cron_generate_daily_plan(ctx) -> None:
    """cron 入口：为每个 persona bot 生成今天的日计划"""
    from app.orm.crud import get_all_persona_ids

    for persona_id in await get_all_persona_ids():
        try:
            await generate_daily_plan(persona_id=persona_id)
        except Exception as e:
            logger.error(f"[{persona_id}] Daily plan generation failed: {e}", exc_info=True)


# ==================== 月计划生成 ====================


async def generate_monthly_plan(
    target_date: date | None = None, persona_id: str = "akao"
) -> str | None:
    """生成月度计划

    给出本月的生活方向和兴趣倾向，不要太具体。
    像一个真人月初时对这个月的模糊预期。

    Args:
        target_date: 目标月份的某一天，默认今天
        persona_id: persona 标识，用于加载对应人设

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
    existing = await get_plan_for_period("monthly", period_start, period_end, persona_id)
    if existing:
        logger.info(f"Monthly plan already exists for {period_start}~{period_end}, skip")
        return existing.content

    # 上下文：上月计划
    prev_plan = await get_latest_plan("monthly", period_start, persona_id)
    prev_plan_text = prev_plan.content if prev_plan else "（这是第一个月计划）"

    season = _get_season(month_start.month)
    month_cn = f"{month_start.year}年{month_start.month}月"

    # 获取人设和 Langfuse prompt 并编译
    prompt_template = get_prompt("schedule_monthly")
    compiled = prompt_template.compile(
        persona_core=await _get_persona_core_for_bot(persona_id),
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
        persona_id=persona_id,
        content=content,
        model=_schedule_model(),
    ))

    logger.info(f"Monthly plan generated: {month_cn}, {len(content)} chars")
    return content


# ==================== 周计划生成 ====================


async def generate_weekly_plan(
    target_date: date | None = None, persona_id: str = "akao"
) -> str | None:
    """生成周计划

    基于月计划给出本周的大致安排。
    像一个真人周末时对下周的模糊规划。

    Args:
        target_date: 目标周的某一天，默认今天
        persona_id: persona 标识，用于加载对应人设

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
    existing = await get_plan_for_period("weekly", period_start, period_end, persona_id)
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
    monthly = await get_plan_for_period("monthly", month_start, month_end_d.isoformat(), persona_id)
    monthly_text = monthly.content if monthly else "（暂无月计划）"

    # 2. 上周计划
    prev_plan = await get_latest_plan("weekly", period_start, persona_id)
    prev_plan_text = prev_plan.content if prev_plan else "（这是第一个周计划）"

    week_desc = f"{period_start}（{_WEEKDAY_CN[week_start.weekday()]}）~ {period_end}（{_WEEKDAY_CN[week_end.weekday()]}）"

    # 获取人设和 Langfuse prompt 并编译
    prompt_template = get_prompt("schedule_weekly")
    compiled = prompt_template.compile(
        persona_core=await _get_persona_core_for_bot(persona_id),
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
        persona_id=persona_id,
        content=content,
        model=_schedule_model(),
    ))

    logger.info(f"Weekly plan generated: {week_desc}, {len(content)} chars")
    return content


# ==================== 日计划生成 ====================


async def generate_daily_plan(
    target_date: date | None = None, persona_id: str = "akao"
) -> str | None:
    """生成日计划（手帐式 markdown）

    三 Agent 管线：Ideation（搜素材）→ Writer（写手帐）→ Critic（审查质量）
    Critic 不通过则 Writer 重写，最多 2 轮。
    """
    if target_date is None:
        target_date = date.today()

    date_str = target_date.isoformat()

    # 检查是否已有
    existing = await get_plan_for_period("daily", date_str, date_str, persona_id)
    if existing:
        logger.info(f"Daily plan already exists for {date_str}, skip")
        return existing.content

    # ---- 收集上下文 ----
    persona_core = await _get_persona_core_for_bot(persona_id)

    # 周计划
    week_start = target_date - timedelta(days=target_date.weekday())
    week_end = week_start + timedelta(days=6)
    weekly = await get_plan_for_period("weekly", week_start.isoformat(), week_end.isoformat(), persona_id)
    weekly_text = weekly.content if weekly else "（暂无周计划）"

    # 昨天 Journal
    yesterday = (target_date - timedelta(days=1)).isoformat()
    yesterday_journal_entry = await get_journal("daily", yesterday, persona_id)
    yesterday_journal = yesterday_journal_entry.content if yesterday_journal_entry else "（昨天没有写日志）"

    # 前 3 天 schedule（Ideation 和 Critic 共用）
    recent = await _get_recent_daily_schedules(target_date, persona_id)
    recent_schedules_text = "\n\n---\n\n".join(
        f"[{s.period_start}]\n{s.content}" for s in recent
    ) if recent else "（没有前几天的日程）"

    # ---- Ideation Agent ----
    try:
        ideation_output = await _run_ideation(
            recent_schedules_text=recent_schedules_text,
            target_date=target_date,
        )
    except Exception as e:
        logger.warning(f"Ideation agent failed, degrading: {e}", exc_info=True)
        ideation_output = ""

    # ---- Writer → Critic 循环 ----
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
        )

        if "PASS" in critic_result:
            logger.info(f"Daily plan passed critic on attempt {attempt + 1}")
            break

        logger.info(f"Daily plan critic rejected (attempt {attempt + 1}): {critic_result[:100]}")
        previous_output = schedule_text
        feedback = critic_result

    if not schedule_text:
        logger.warning(f"Pipeline produced empty daily plan for {date_str}")
        return None

    # ---- 存储 ----
    await upsert_schedule(AkaoSchedule(
        plan_type="daily",
        period_start=date_str,
        period_end=date_str,
        persona_id=persona_id,
        content=schedule_text,
        model="offline-model",
    ))

    logger.info(f"Daily plan generated for {date_str}: {len(schedule_text)} chars")
    return schedule_text


# ==================== 辅助函数 ====================


def _extract_text(content) -> str:
    """从 LLM 响应中提取文本"""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return content or ""


