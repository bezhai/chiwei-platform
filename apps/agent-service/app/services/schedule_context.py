"""日程上下文构建

查询今日的手帐/备忘录，注入 system prompt。
让赤尾知道自己「今天在过什么样的日子」。
"""

import logging
from datetime import datetime, timedelta, timezone

from app.orm.crud import get_plan_for_period

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


async def build_schedule_context() -> str:
    """构建当前的日程上下文，注入 system prompt

    只注入今日手帐内容。月/周计划不直接注入聊天，
    它们的作用是在生成日计划时提供方向。

    Returns:
        今日手帐文本，空字符串表示无数据
    """
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")

    daily = await get_plan_for_period("daily", today, today)
    if not daily:
        return ""

    return daily.content
