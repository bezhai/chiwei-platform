"""第一层：赤尾的内心状态

基于当前时间和日程，构建赤尾此刻的内心状态。
Phase 1 为简单占位，Phase 2 接入生活引擎后丰富。
"""

import logging
from datetime import datetime, timedelta, timezone

from app.orm.crud import get_plan_for_period

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

_TIME_VIBES = {
    (0, 7): "深夜，有点迷迷糊糊的",
    (7, 10): "刚醒来，还没完全清醒",
    (10, 12): "上午，精力还不错",
    (12, 14): "中午，刚吃完饭有点犯困",
    (14, 17): "下午，状态一般般",
    (17, 19): "傍晚，精力开始恢复",
    (19, 22): "晚上，比较放松",
    (22, 24): "夜里，有点困但还不想睡",
}

_WEEKDAY_VIBES = {
    0: "周一",
    1: "周二",
    2: "周三",
    3: "周四",
    4: "周五，快周末了",
    5: "周六，休息日",
    6: "周日，明天又要上班了",
}


def _get_time_vibe(hour: int) -> str:
    for (start, end), vibe in _TIME_VIBES.items():
        if start <= hour < end:
            return vibe
    return "深夜"


async def build_inner_state() -> str:
    """构建赤尾此刻的内心状态（第一层）

    Phase 1: 基于时间 + 今日手帐
    Phase 2: 接入生活引擎（天气、番剧、音乐等）

    Returns:
        内心状态文本，约 100-200 tokens
    """
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")
    hour = now.hour
    weekday = now.weekday()

    time_vibe = _get_time_vibe(hour)
    weekday_vibe = _WEEKDAY_VIBES.get(weekday, "")

    daily = await get_plan_for_period("daily", today, today)

    if daily and daily.content:
        mood_hint = f"（心情：{daily.mood}）" if daily.mood else ""
        return f"现在是{weekday_vibe}的{time_vibe}。{mood_hint}\n{daily.content}"

    return f"现在是{weekday_vibe}的{time_vibe}。今天没什么特别的安排。"
