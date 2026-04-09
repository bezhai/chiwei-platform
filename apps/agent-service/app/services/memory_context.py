"""赤尾聊天注入上下文 v4

Life Engine 状态 + 最近碎片（精简）作为运行时上下文。
Schedule、daily dream 由 Life Engine 消化，不再直接注入。
"""

import logging
from datetime import timedelta, timezone

from app.orm.memory_crud import get_today_fragments

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# 最近碎片：只保留最近 2 条，总计不超过 800 字符
_MAX_RECENT_FRAGMENTS = 2
_MAX_FRAGMENT_CHARS = 800


async def _build_life_state(persona_id: str) -> str:
    """从 DB 读取 Life Engine 状态，返回注入文本"""
    try:
        from app.services.life_engine import _load_state

        row = await _load_state(persona_id)
        if not row:
            return ""
        current = row.current_state
        mood = row.response_mood
        if current:
            return f"你此刻的状态：{current}\n你的心情：{mood}" if mood else f"你此刻的状态：{current}"
    except Exception as e:
        logger.warning(f"[{persona_id}] Failed to read life state: {e}")
    return ""


async def build_inner_context(
    chat_id: str,
    chat_type: str,
    user_ids: list[str],
    trigger_user_id: str,
    trigger_username: str,
    persona_id: str,
    chat_name: str = "",
    *,
    is_proactive: bool = False,
    proactive_stimulus: str = "",
) -> str:
    sections: list[str] = []

    # === 场景提示 ===
    if is_proactive:
        scene = f"你在群聊「{chat_name}」中。" if chat_name else ""
        scene += "\n你刚刷到了群里的对话。如果你想说点什么就说，不想说也可以不说。"
        scene += "\n不要刻意解释为什么突然说话，像朋友在群里自然接话就好。"
        if proactive_stimulus:
            scene += f"\n（你注意到的：{proactive_stimulus}）"
        sections.append(scene)
    elif chat_type == "p2p":
        if trigger_username:
            sections.append(f"你正在和 {trigger_username} 私聊。")
    else:
        if chat_name:
            sections.append(f"你在群聊「{chat_name}」中。")
        if trigger_username:
            sections.append(f"需要回复 {trigger_username} 的消息（消息中用 ⭐ 标记）。")

    # === 此刻的状态（Life Engine）===
    life_state = await _build_life_state(persona_id)
    if life_state:
        sections.append(life_state)

    # === 关系记忆（对当前对话者的印象）===
    if trigger_user_id and trigger_user_id != "__proactive__":
        from app.orm.memory_crud import get_latest_relationship_memory
        from app.orm.crud import get_username

        rel_memory = await get_latest_relationship_memory(persona_id, trigger_user_id)
        if rel_memory:
            core_facts, impression = rel_memory
            name = trigger_username or await get_username(trigger_user_id) or trigger_user_id[:6]
            parts = [f"关于 {name}："]
            if core_facts:
                parts.append(f"[事实] {core_facts}")
            if impression:
                parts.append(f"[印象] {impression}")
            sections.append("\n".join(parts))

    # === 最近的经历（精简版：最近 2 条碎片，短期记忆）===
    today_frags = await get_today_fragments(persona_id, grains=["conversation"])
    if chat_type == "group":
        today_frags = [f for f in today_frags if f.source_chat_id == chat_id]
    if today_frags:
        recent = today_frags[-_MAX_RECENT_FRAGMENTS:]
        total = 0
        lines = []
        for f in recent:
            text = f.content.strip()
            if total + len(text) > _MAX_FRAGMENT_CHARS:
                remaining = _MAX_FRAGMENT_CHARS - total
                if remaining > 50:
                    lines.append(text[:remaining] + "...")
                break
            lines.append(text)
            total += len(text)
        if lines:
            sections.append(f"最近的经历：\n{''.join(lines)}")

    # === 回忆引导 ===
    sections.append("（如果隐约觉得知道点什么但想不起来，可以用 recall 想一想。）")

    return "\n\n".join(sections)


# 向后兼容别名
build_memory_context = build_inner_context
