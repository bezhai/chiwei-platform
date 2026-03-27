"""赤尾聊天注入上下文 — 统一 inner_context

构建注入 system prompt 的所有上下文：
- 场景提示（群名/私聊 + 回复谁）
- 今日状态（Journal daily / Schedule daily）
- 对人和群的感觉
- 记忆回溯引导语
"""

import logging
from datetime import datetime, timedelta, timezone

from app.orm.crud import (
    get_cross_group_impressions,
    get_group_culture_gestalt,
    get_impressions_for_users,
    get_journal,
    get_plan_for_period,
    get_username,
)

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
MAX_IMPRESSION_USERS = 10
MAX_CROSS_GROUP_IMPRESSIONS = 5

_MEMORY_RECALL_HINT = (
    "（你有写日记的习惯。如果聊着聊着觉得\u201c这个事我好像知道点什么但记不清了\u201d，"
    "可以翻翻日记想一想。）"
)


async def _build_today_state() -> str:
    """构建今日状态：优先 Journal daily，fallback Schedule daily"""
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")

    # 优先用 Journal（模糊化的个人感受）
    journal = await get_journal("daily", today)
    if not journal:
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        journal = await get_journal("daily", yesterday)

    if journal:
        return journal.content

    # fallback: Schedule daily
    schedule = await get_plan_for_period("daily", today, today)
    if schedule and schedule.content:
        return schedule.content

    return ""


async def build_inner_context(
    chat_id: str,
    chat_type: str,
    user_ids: list[str],
    trigger_user_id: str,
    trigger_username: str,
    chat_name: str = "",
) -> str:
    """构建统一的聊天注入上下文

    Args:
        chat_id: 群/私聊 ID
        chat_type: "group" 或 "p2p"
        user_ids: 当前对话中出现的用户 ID 列表
        trigger_user_id: 触发者 user_id
        trigger_username: 触发者用户名
        chat_name: 群名（群聊场景）

    Returns:
        组装好的 inner_context 文本，注入 system prompt
    """
    sections: list[str] = []

    # === 场景提示 ===
    if chat_type == "p2p":
        if trigger_username:
            sections.append(f"你正在和 {trigger_username} 私聊。")
    else:
        if chat_name:
            sections.append(f"你在群聊「{chat_name}」中。")
        if trigger_username:
            sections.append(f"需要回复 {trigger_username} 的消息（消息中用 ⭐ 标记）。")

    # === 今日状态（Journal / Schedule） ===
    today_state = await _build_today_state()
    if today_state:
        sections.append(f"你今天的状态：\n{today_state}")

    # === 对人和群的感觉 ===
    if chat_type == "group":
        group_gestalt = await get_group_culture_gestalt(chat_id)
        if group_gestalt:
            sections.append(f"你对这个群的感觉：{group_gestalt}")

        if user_ids:
            people_lines = await _build_people_gestalt(chat_id, user_ids)
            if people_lines:
                sections.append(
                    "你对当前对话中出现的人的感觉：\n" + "\n".join(people_lines)
                )
    else:
        cross_lines = await _build_cross_group_gestalt(
            trigger_user_id, trigger_username
        )
        if cross_lines:
            sections.append(cross_lines)

    # === 记忆回溯引导语 ===
    sections.append(_MEMORY_RECALL_HINT)

    return "\n\n".join(sections)


async def _build_people_gestalt(chat_id: str, user_ids: list[str]) -> list[str]:
    """构建对话者的感觉 gestalt 列表"""
    impressions = await get_impressions_for_users(
        chat_id, user_ids[:MAX_IMPRESSION_USERS]
    )
    if not impressions:
        return []
    lines = []
    for imp in impressions:
        name = await get_username(imp.user_id) or imp.user_id[:8]
        lines.append(f"- {name}：{imp.impression_text}")
    return lines


async def _build_cross_group_gestalt(user_id: str, trigger_username: str) -> str:
    """构建跨群人物 gestalt（私聊场景）"""
    rows = await get_cross_group_impressions(
        user_id, limit=MAX_CROSS_GROUP_IMPRESSIONS
    )
    if not rows:
        return ""
    lines = []
    for imp, group_name in rows:
        lines.append(f"- （{group_name}）{imp.impression_text}")
    return f"你对 {trigger_username} 的感觉：\n" + "\n".join(lines)


# 向后兼容别名
build_memory_context = build_inner_context
