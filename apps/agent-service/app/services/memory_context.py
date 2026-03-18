"""记忆上下文构建服务

从日记 / 周记 / 人物印象构建记忆文本，注入 system prompt。
"""

import logging
from datetime import date

from app.orm.crud import (
    get_cross_group_impressions,
    get_impressions_for_users,
    get_latest_weekly_review,
    get_recent_diaries,
    get_username,
)

logger = logging.getLogger(__name__)

# 日记+周记注入硬上限
MAX_DIARY_CHARS = 2000


MAX_IMPRESSION_USERS = 10
MAX_CROSS_GROUP_IMPRESSIONS = 5


async def build_impression_context(chat_id: str, user_ids: list[str]) -> str:
    """构建人物印象文本，注入群聊 system prompt"""
    if not user_ids:
        return ""
    impressions = await get_impressions_for_users(chat_id, user_ids[:MAX_IMPRESSION_USERS])
    if not impressions:
        return ""
    lines = []
    for imp in impressions:
        name = await get_username(imp.user_id) or imp.user_id[:8]
        lines.append(f"【{name}】{imp.impression_text}")
    return "你对群友的印象：\n" + "\n".join(lines)


async def build_diary_context(chat_id: str) -> str:
    """构建日记+周记记忆文本，注入群聊 system prompt

    - 最近 2 篇日记（鲜明记忆）
    - 最近 1 篇周记（褪色记忆）
    """
    today = date.today().isoformat()

    # 最近 2 篇日记
    diaries = await get_recent_diaries(chat_id, today, limit=2)
    if not diaries:
        return ""
    parts = []
    for diary in reversed(diaries):  # DB 返回 desc，reversed 变正序（旧→新）
        parts.append(f"--- {diary.diary_date} ---\n{diary.content}")

    # 最近 1 篇周记
    weekly = await get_latest_weekly_review(chat_id, today, limit=1)
    if weekly:
        w = weekly[0]
        parts.append(f"--- 上周回顾 ({w.week_start} ~ {w.week_end}) ---\n{w.content}")

    text = "\n\n".join(parts)
    if len(text) > MAX_DIARY_CHARS:
        text = text[:MAX_DIARY_CHARS] + "……"
    return text


async def build_cross_group_impression_context(
    user_id: str, trigger_username: str
) -> str:
    """构建跨群人物印象文本，注入私聊 system prompt

    从所有群聊中取该用户的印象（JOIN lark_group_chat_info 自然过滤 P2P），
    按 updated_at 倒序取最多 5 条，标注来源群名。
    """
    rows = await get_cross_group_impressions(user_id, limit=MAX_CROSS_GROUP_IMPRESSIONS)
    if not rows:
        return ""
    lines = []
    for imp, group_name in rows:
        lines.append(f"【{group_name}】{imp.impression_text}")
    return f"你在群聊中对 {trigger_username} 的印象：\n" + "\n".join(lines)
