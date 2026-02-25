"""记忆上下文构建服务

从 user_knowledge 表读取用户知识，渲染为自然语言文本注入 system prompt。
"""

import logging
from collections import defaultdict

from app.orm.crud import get_user_knowledge
from app.orm.models import UserKnowledge

logger = logging.getLogger(__name__)

# 硬上限：约 600 tokens，中文 ~1.5 token/字 → ~400 字
MAX_CHARS = 400

# confidence 最低展示阈值
_MIN_CONFIDENCE = 0.6


async def build_memory_context(user_id: str, chat_id: str, chat_type: str) -> str:
    """构建记忆上下文文本，注入 system prompt

    Args:
        user_id: 触发用户 ID
        chat_id: 聊天 ID（预留）
        chat_type: 聊天类型

    Returns:
        渲染后的记忆文本，或空字符串
    """
    knowledge = await get_user_knowledge(user_id)
    if not knowledge:
        return ""

    return _render_knowledge(knowledge)


def _render_knowledge(knowledge: UserKnowledge) -> str:
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
        parts.append(f"你对这个人的印象：{knowledge.personality_note}")

    # communication_style
    if knowledge.communication_style:
        parts.append(f"沟通风格：{knowledge.communication_style}")

    text = "\n".join(parts)

    # 硬上限截断
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "……"

    return text
