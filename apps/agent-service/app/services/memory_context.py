"""赤尾聊天注入上下文 v4

Life Engine 状态是唯一的运行时上下文来源。
Schedule、碎片、daily dream 由 Life Engine 消化，不再直接注入。
"""

import logging
from datetime import timedelta, timezone

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


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

    # === 此刻的状态（Life Engine — 唯一的运行时上下文来源）===
    life_state = await _build_life_state(persona_id)
    if life_state:
        sections.append(life_state)

    return "\n\n".join(sections)


async def get_reply_style(persona_id: str, default_style: str = "") -> str:
    """获取 reply_style：DB 最新记录 → DB 默认值"""
    from app.orm.memory_crud import get_latest_reply_style

    latest = await get_latest_reply_style(persona_id)
    if latest:
        return latest
    return default_style


# 向后兼容别名
build_memory_context = build_inner_context
