"""记忆上下文构建服务 — 三层架构

第一层：赤尾的内心状态（始终存在，~200 tokens）
第二层：对人和群的感觉 gestalt（按场景加载，~200 tokens）
第三层：自然联想（对话中通过 load_memory 按需触发，不在此处注入）
"""

import logging

from app.orm.crud import (
    get_cross_group_impressions,
    get_group_culture_gestalt,
    get_impressions_for_users,
    get_username,
)
from app.services.inner_state import build_inner_state

logger = logging.getLogger(__name__)

MAX_IMPRESSION_USERS = 10
MAX_CROSS_GROUP_IMPRESSIONS = 5


async def build_memory_context(
    chat_id: str,
    chat_type: str,
    user_ids: list[str],
    trigger_user_id: str,
    trigger_username: str,
) -> str:
    """构建三层记忆上下文（新入口）

    Args:
        chat_id: 群/私聊 ID
        chat_type: "group" 或 "p2p"
        user_ids: 当前对话中出现的用户 ID 列表
        trigger_user_id: 触发者 user_id
        trigger_username: 触发者用户名

    Returns:
        组装好的记忆上下文文本，注入 system prompt
    """
    sections = []

    # === 第一层：赤尾的内心 ===
    inner = await build_inner_state()
    if inner:
        sections.append(f"你现在的内心：\n{inner}")

    # === 第二层：对人和群的感觉 ===
    if chat_type == "group":
        # 群感觉
        group_gestalt = await get_group_culture_gestalt(chat_id)
        if group_gestalt:
            sections.append(f"你对这个群的感觉：{group_gestalt}")

        # 对话者的感觉
        if user_ids:
            people_lines = await _build_people_gestalt(chat_id, user_ids)
            if people_lines:
                sections.append(
                    "你对当前对话中出现的人的感觉：\n" + "\n".join(people_lines)
                )
    else:
        # 私聊：跨群印象
        cross_lines = await _build_cross_group_gestalt(
            trigger_user_id, trigger_username
        )
        if cross_lines:
            sections.append(cross_lines)

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


# === 向后兼容别名（Task 6 更新 agent.py 后删除） ===


async def build_diary_context(chat_id: str) -> str:  # noqa: ARG001
    """已废弃 — 三层架构不再注入日记全文"""
    logger.warning("build_diary_context is deprecated, use build_memory_context")
    return ""


async def build_impression_context(chat_id: str, user_ids: list[str]) -> str:
    """已废弃 — 由 build_memory_context 第二层替代"""
    logger.warning("build_impression_context is deprecated, use build_memory_context")
    return ""


async def build_cross_group_impression_context(
    user_id: str, trigger_username: str
) -> str:
    """已废弃 — 由 build_memory_context 第二层替代"""
    logger.warning(
        "build_cross_group_impression_context is deprecated, use build_memory_context"
    )
    return ""
