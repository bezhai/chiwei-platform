"""日程上下文构建

查询当前时刻的月/周/日计划，组装为自然语言注入 system prompt。
让赤尾知道自己「现在在干什么」「这周/这个月的生活状态」。
"""

import logging
from datetime import datetime, timedelta, timezone

from app.orm.crud import get_current_schedule

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 精力值对应的描述
_ENERGY_DESC = {
    1: "很困，精力很低",
    2: "有点疲惫",
    3: "状态一般",
    4: "精力不错",
    5: "精力充沛，状态很好",
}


async def build_schedule_context() -> str:
    """构建当前时刻的日程上下文，注入 system prompt

    Returns:
        格式化后的日程文本，空字符串表示无日程数据
    """
    now = datetime.now(CST)
    now_date = now.strftime("%Y-%m-%d")
    now_time = now.strftime("%H:%M")
    weekday = now.weekday()
    weekday_cn = _WEEKDAY_CN[weekday]
    hour = now.hour

    entries = await get_current_schedule(now_date, now_time, weekday)
    if not entries:
        return ""

    parts: list[str] = []

    # 时间感知
    time_desc = _time_of_day_desc(hour)
    parts.append(f"现在是{now_date}（{weekday_cn}）{now_time}，{time_desc}。")

    # 按 plan_type 分组
    monthly = None
    weekly = None
    daily = None
    for entry in entries:
        if entry.plan_type == "monthly" and not monthly:
            monthly = entry
        elif entry.plan_type == "weekly" and not weekly:
            weekly = entry
        elif entry.plan_type == "daily" and not daily:
            daily = entry

    # 月度方向（简短）
    if monthly:
        parts.append(f"这个月的状态：{monthly.content}")

    # 周计划
    if weekly:
        parts.append(f"这周：{weekly.content}")

    # 日计划时段（最重要，直接影响回复风格）
    if daily:
        parts.append(f"你现在正在：{daily.content}")
        if daily.mood:
            parts.append(f"心情：{daily.mood}")
        if daily.energy_level:
            parts.append(f"精力：{_ENERGY_DESC.get(daily.energy_level, '')}")
        if daily.response_style_hint:
            parts.append(f"（{daily.response_style_hint}）")

    return "\n".join(parts)


def _time_of_day_desc(hour: int) -> str:
    """根据小时返回时段描述"""
    if hour < 6:
        return "深夜/凌晨"
    elif hour < 9:
        return "早上"
    elif hour < 12:
        return "上午"
    elif hour < 14:
        return "中午"
    elif hour < 18:
        return "下午"
    elif hour < 21:
        return "晚上"
    else:
        return "深夜"
