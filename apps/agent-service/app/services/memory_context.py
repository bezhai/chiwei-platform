"""记忆上下文构建服务

构建注入 system prompt 的上下文片段。
新设计中聊天上下文只注入: persona + Schedule(today) + ChatImpression + PersonImpression
历史日记/日志不直接注入，通过 Schedule 间接传递。
"""

import logging

from app.orm.crud import (
    get_chat_impression,
    get_cross_group_impressions,
    get_impressions_for_users,
    get_username,
)

logger = logging.getLogger(__name__)


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


async def build_chat_impression_context(chat_id: str) -> str:
    """构建群氛围印象文本，注入 system prompt

    描述这个群/聊天场的整体感觉：氛围、节奏、她在其中的位置。
    """
    impression = await get_chat_impression(chat_id)
    if not impression:
        return ""
    return f"你对这个群的感觉：\n{impression.impression_text}"
