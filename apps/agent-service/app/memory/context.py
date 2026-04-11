"""Memory context builder — assembles the inner context injected into chat prompts.

Sections:
  - Scene prompt (group/p2p/proactive)
  - Life Engine state (current activity + mood)
  - Relationship memory (core_facts + impression for trigger user)
  - Recent experience fragments (last 2 conversation fragments)
  - Recall hint
"""

from __future__ import annotations

import logging
from datetime import timedelta, timezone

from app.data.queries import (
    find_latest_life_state,
    find_latest_relationship_memory,
    find_today_fragments,
    find_username,
)
from app.data.session import get_session

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))

_MAX_RECENT_FRAGMENTS = 2
_MAX_FRAGMENT_CHARS = 800


async def _build_life_state(persona_id: str) -> str:
    """Read Life Engine state from DB, return injection text."""
    try:
        async with get_session() as s:
            row = await find_latest_life_state(s, persona_id)
        if not row:
            return ""
        current = row.current_state
        mood = row.response_mood
        if current:
            return f"你此刻的状态：{current}\n你的心情：{mood}" if mood else f"你此刻的状态：{current}"
    except Exception as e:
        logger.warning("[%s] Failed to read life state: %s", persona_id, e)
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
    """Assemble the full inner context string for chat injection."""
    sections: list[str] = []

    # === Scene ===
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

    # === Life Engine state ===
    life_state = await _build_life_state(persona_id)
    if life_state:
        sections.append(life_state)

    # === Relationship memory ===
    if trigger_user_id and trigger_user_id != "__proactive__":
        async with get_session() as s:
            rel_memory = await find_latest_relationship_memory(s, persona_id, trigger_user_id)
        if rel_memory:
            core_facts, impression = rel_memory
            if not trigger_username:
                async with get_session() as s:
                    trigger_username = await find_username(s, trigger_user_id) or trigger_user_id[:6]
            parts = [f"关于 {trigger_username}："]
            if core_facts:
                parts.append(f"[事实] {core_facts}")
            if impression:
                parts.append(f"[印象] {impression}")
            sections.append("\n".join(parts))

    # === Recent fragments ===
    async with get_session() as s:
        today_frags = await find_today_fragments(s, persona_id, grains=["conversation"])
    if chat_type == "group":
        today_frags = [f for f in today_frags if f.source_chat_id == chat_id]
    if today_frags:
        recent = today_frags[-_MAX_RECENT_FRAGMENTS:]
        total = 0
        lines: list[str] = []
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

    # === Recall hint ===
    sections.append("（如果隐约觉得知道点什么但想不起来，可以用 recall 想一想。）")

    return "\n\n".join(sections)


# Backward-compatible alias
build_memory_context = build_inner_context
