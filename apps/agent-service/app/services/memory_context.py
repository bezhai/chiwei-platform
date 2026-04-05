"""赤尾聊天注入上下文 v3

基于 experience_fragment 构建 system prompt 注入的所有上下文。
"""

import logging
from datetime import datetime, timedelta, timezone

from app.orm.crud import get_plan_for_period
from app.orm.memory_crud import get_recent_fragments_by_grain, get_today_fragments
from app.services.identity_drift import get_base_reply_style, get_identity_state

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

MAX_FRAGMENT_SECTION_CHARS = 3000  # ~2000 tokens
MAX_DISTANT_SECTION_CHARS = 800   # ~500 tokens

_MEMORY_RECALL_HINT = (
    "（你有写日记的习惯。如果隐约觉得知道点什么但想不起来，可以用 recall 想一想。"
    "如果想确认某件事具体怎么说的，可以翻翻聊天记录。）"
)


async def _build_today_state(persona_id: str) -> str:
    """今天 Schedule（Life Engine 接入前的替代方案）"""
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")
    schedule = await get_plan_for_period("daily", today, today, persona_id)
    if schedule and schedule.content:
        return schedule.content
    return ""


def _filter_fragments_for_group(fragments: list, current_chat_id: str) -> list:
    """群聊场景：只保留当前群的 conversation/glimpse 碎片"""
    return [f for f in fragments if f.source_chat_id == current_chat_id]


def _format_fragment_section(fragments: list, max_chars: int) -> str:
    """格式化碎片列表为文本，超出截断"""
    if not fragments:
        return ""
    lines = []
    total = 0
    for f in fragments:
        text = f.content.strip()
        if total + len(text) > max_chars:
            remaining = max_chars - total
            if remaining > 50:
                lines.append(text[:remaining] + "...")
            break
        lines.append(text)
        total += len(text)
    return "\n\n".join(lines)


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

    # === 场景提示 === (KEEP SAME)
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

    # === 今日基调 ===
    today_state = await _build_today_state(persona_id)
    if today_state:
        sections.append(f"你今天的基调：\n{today_state}")

    # === 脑子里的东西（今天的经历碎片）===
    today_frags = await get_today_fragments(persona_id, grains=["conversation", "glimpse"])

    if chat_type == "group":
        visible_frags = _filter_fragments_for_group(today_frags, chat_id)
    else:
        visible_frags = today_frags

    if visible_frags:
        frag_text = _format_fragment_section(visible_frags, MAX_FRAGMENT_SECTION_CHARS)
        if frag_text:
            sections.append(f"脑子里的东西（今天的经历）：\n{frag_text}")

    # === 更远的记忆 ===
    distant_frags = await get_recent_fragments_by_grain(persona_id, "daily", limit=3)
    if distant_frags:
        distant_text = _format_fragment_section(distant_frags, MAX_DISTANT_SECTION_CHARS)
        if distant_text:
            sections.append(f"更远的记忆：\n{distant_text}")

    # === 记忆回溯引导 ===
    sections.append(_MEMORY_RECALL_HINT)

    return "\n\n".join(sections)


async def get_reply_style(chat_id: str, persona_id: str, default_style: str = "") -> str:
    """获取动态 reply-style：per-chat 漂移 → 全局基线 → DB 默认"""
    try:
        drift_state = await get_identity_state(chat_id, persona_id)
        if drift_state:
            return drift_state
    except Exception:
        pass
    try:
        base_state = await get_base_reply_style(persona_id)
        if base_state:
            return base_state
    except Exception:
        pass
    return default_style


# 向后兼容别名
build_memory_context = build_inner_context
