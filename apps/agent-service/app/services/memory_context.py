"""记忆上下文构建服务

从 user_knowledge 表读取用户知识，渲染为自然语言文本注入 system prompt。
"""

import logging
from collections import defaultdict
from datetime import date

from app.orm.crud import (
    get_impressions_for_users,
    get_latest_weekly_review,
    get_recent_diaries,
    get_user_knowledge,
    get_username,
)
from app.orm.models import UserKnowledge

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

# 硬上限：约 600 tokens，中文 ~1.5 token/字 → ~400 字
MAX_CHARS = 400

# confidence 最低展示阈值
_MIN_CONFIDENCE = 0.6


async def build_memory_context(
    user_id: str, chat_id: str, chat_type: str, *, username: str = ""
) -> str:
    """构建记忆上下文文本，注入 system prompt

    Args:
        user_id: 触发用户 ID
        chat_id: 聊天 ID（预留）
        chat_type: 聊天类型
        username: 触发用户名，用于画像归属

    Returns:
        渲染后的记忆文本，或空字符串
    """
    knowledge = await get_user_knowledge(user_id)
    if not knowledge:
        return ""

    return _render_knowledge(knowledge, username=username)


def _render_knowledge(knowledge: UserKnowledge, *, username: str = "") -> str:
    """将 UserKnowledge 渲染为赤尾视角的自然语言描述"""
    parts: list[str] = []

    # 按 category 分组渲染 facts
    facts = knowledge.facts or []
    if facts:
        # 过滤低置信度
        strong_facts = [f for f in facts if f.get("confidence", 0) >= _MIN_CONFIDENCE]

        if strong_facts:
            grouped: dict[str, list[str]] = defaultdict(list)
            for fact in strong_facts:
                category = fact.get("category", "其他")
                content = fact.get("content", "")
                if content:
                    grouped[category].append(content)

            # 按固定顺序渲染
            category_order = [
                "基本信息",
                "职业",
                "爱好",
                "习惯",
                "偏好",
                "人际关系",
                "近况",
                "其他",
            ]
            for cat in category_order:
                items = grouped.get(cat)
                if items:
                    parts.append(f"{cat}：{'；'.join(items)}。")

            # 处理不在预定义顺序中的 category
            for cat, items in grouped.items():
                if cat not in category_order and items:
                    parts.append(f"{cat}：{'；'.join(items)}。")

    # personality_note
    if knowledge.personality_note:
        label = f"你对 {username} 的印象" if username else "你的印象"
        parts.append(f"{label}：{knowledge.personality_note}")

    # communication_style
    if knowledge.communication_style:
        parts.append(f"沟通风格：{knowledge.communication_style}")

    text = "\n".join(parts)

    # 硬上限截断
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "……"

    return text
