"""Memory context builder v4 — assemble always-on + conditional sections.

Sections (order matters for prompt flow):
  1. Scene (p2p/group/proactive)
  2. Life state (current activity + mood)
  3. Today schedule
  4. Self abstracts (subject='self')
  5. User abstracts (subject='user:<id>' / 和 X 的关系)  — skipped if no trigger_user
  6. Active notes
  7. Cross-chat (user-centric raw msgs)  — skipped if no trigger_user
  8. Short-term fragments (§2.8)
  9. Recall index (counts + recent titles)
"""

from __future__ import annotations

import logging
from datetime import timedelta, timezone

from app.data.queries import find_latest_life_state
from app.data.session import get_session
from app.memory.sections.active_notes import build_active_notes_section
from app.memory.sections.recall_index import build_recall_index_section
from app.memory.sections.schedule import build_schedule_section
from app.memory.sections.self_abstracts import build_self_abstracts_section
from app.memory.sections.short_term_fragments import build_short_term_fragments_section
from app.memory.sections.user_abstracts import build_user_abstracts_section

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))


async def _build_life_state(persona_id: str) -> str:
    try:
        async with get_session() as s:
            row = await find_latest_life_state(s, persona_id)
        if not row:
            return ""
        current = row.current_state
        mood = row.response_mood
        if current:
            return (
                f"你此刻的状态：{current}\n你的心情：{mood}"
                if mood else f"你此刻的状态：{current}"
            )
    except Exception as e:
        logger.warning("[%s] Failed to read life state: %s", persona_id, e)
    return ""


def _scene_section(
    chat_type: str,
    chat_name: str,
    trigger_username: str | None,
    is_proactive: bool,
    proactive_stimulus: str,
) -> str:
    if is_proactive:
        scene = f"你在群聊「{chat_name}」中。" if chat_name else ""
        scene += "\n你刚刷到了群里的对话。如果你想说点什么就说，不想说也可以不说。"
        scene += "\n不要刻意解释为什么突然说话，像朋友在群里自然接话就好。"
        if proactive_stimulus:
            scene += f"\n（你注意到的：{proactive_stimulus}）"
        return scene
    if chat_type == "p2p":
        return f"你正在和 {trigger_username} 私聊。" if trigger_username else ""
    parts = []
    if chat_name:
        parts.append(f"你在群聊「{chat_name}」中。")
    if trigger_username:
        parts.append(f"需要回复 {trigger_username} 的消息（消息中用 ⭐ 标记）。")
    return "\n".join(parts)


async def build_inner_context(
    chat_id: str,
    chat_type: str,
    user_ids: list[str],
    trigger_user_id: str | None,
    trigger_username: str | None,
    persona_id: str,
    chat_name: str = "",
    *,
    is_proactive: bool = False,
    proactive_stimulus: str = "",
) -> str:
    """Assemble the full inner context string for chat injection (v4)."""

    effective_user_id = (
        None if (trigger_user_id in (None, "__proactive__")) else trigger_user_id
    )

    sections: list[str] = []

    scene = _scene_section(
        chat_type, chat_name, trigger_username, is_proactive, proactive_stimulus
    )
    if scene:
        sections.append(scene)

    life = await _build_life_state(persona_id)
    if life:
        sections.append(life)

    sched = await build_schedule_section(persona_id=persona_id)
    if sched:
        sections.append(sched)

    self_abs = await build_self_abstracts_section(persona_id=persona_id)
    if self_abs:
        sections.append(self_abs)

    user_abs = await build_user_abstracts_section(
        persona_id=persona_id,
        trigger_user_id=effective_user_id,
        trigger_username=trigger_username,
    )
    if user_abs:
        sections.append(user_abs)

    notes = await build_active_notes_section(persona_id=persona_id)
    if notes:
        sections.append(notes)

    if effective_user_id:
        from app.memory.cross_chat import (
            build_cross_chat_context,  # lazy: avoids circular import
        )

        cross = await build_cross_chat_context(
            persona_id=persona_id,
            trigger_user_id=effective_user_id,
            trigger_username=trigger_username or "",
            current_chat_id=chat_id,
        )
        if cross:
            sections.append(cross)

    frag = await build_short_term_fragments_section(
        persona_id=persona_id,
        chat_id=chat_id,
        trigger_user_id=effective_user_id,
    )
    if frag:
        sections.append(frag)

    recall_idx = await build_recall_index_section(persona_id=persona_id)
    if recall_idx:
        sections.append(recall_idx)

    return "\n\n".join(sections)
