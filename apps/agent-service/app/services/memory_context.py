"""记忆上下文构建服务

从日记 / 周记 / 人物印象构建记忆文本，注入 system prompt。
"""

import logging
from datetime import date

from app.orm.crud import (
    get_impressions_for_users,
    get_latest_weekly_review,
    get_recent_diaries,
    get_username,
)

logger = logging.getLogger(__name__)

# 日记+周记注入硬上限
MAX_DIARY_CHARS = 2000


MAX_IMPRESSION_USERS = 10


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
