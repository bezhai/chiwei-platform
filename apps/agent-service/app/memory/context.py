"""Inner-context builder — what chat feeds 赤尾 each turn.

Sections, in order:
  1. World-arc awareness (only when the arc exists) — the public life stage
     this family has reached (WorldArc), rendered first-person by
     ``render_arc_awareness``. Leads the block because it changes on a
     day/week clock — stable-prefix before the per-message scene and the
     hour-level life snapshot (prompt-cache friendly). Cold chain / read
     failure → the section is simply absent, no placeholder.
  2. Scene (p2p / group / proactive) — who she's talking to, why.
  3. Life snapshot — where she is, what she's doing, how she feels *right now*,
     read straight from the life engine's LifeState. This is the main subject:
     the 赤尾 talking to a real person is the 赤尾 living this moment.

There is no RAG recall here. She speaks from what her life already knows
(the shared world stage + current_state + mood), nothing more — this keeps
her information boundary (the arc is by writing discipline the progress
everyone present already knows, so reading it adds nothing she wouldn't know).
"""

from __future__ import annotations

import logging

from app.domain.arc_awareness import render_arc_awareness
from app.domain.life_state import find_life_state
from app.runtime.lane_policy import current_deployment_lane

logger = logging.getLogger(__name__)

# Cold start / thin state / read error all land here so inner_context never
# collapses and chat can still hold a normal conversation.
_LIFE_FALLBACK = "你此刻的状态暂时拿不到，就照你平时的样子自然聊吧。"


async def _build_life_state(persona_id: str) -> str:
    """她此刻的真实快照 (LifeState)，作为 inner_context 的主角。

    lane 口径与 world/life 写入端一致：``current_deployment_lane() or "prod"``
    （进程级泳道，prod 归一到 "prod"）。

    失败兜底（spec decision 6）：读不到快照（冷启 / 她还没活过一轮）、
    current_state 稀薄、或读取报错时，返回一句简洁兜底而非空串——
    inner_context 不能塌，chat 仍要能正常对话。
    """
    try:
        lane = current_deployment_lane() or "prod"
        snap = await find_life_state(lane=lane, persona_id=persona_id)
    except Exception as e:
        logger.warning("[%s] Failed to read life state: %s", persona_id, e)
        return _LIFE_FALLBACK

    if not snap:
        return _LIFE_FALLBACK

    current = (snap.current_state or "").strip()
    if not current:
        return _LIFE_FALLBACK

    mood = (snap.response_mood or "").strip()
    if mood:
        return f"你此刻正在：{current}\n你的心情：{mood}"
    return f"你此刻正在：{current}"


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
    """Assemble inner_context: arc awareness (when present) + scene + life snapshot."""

    sections: list[str] = []

    # 世界阶段透传：对话里她也必须知道自己人生走到哪页（世界阶段翻页后 persona
    # 出厂设定可能已过时）。lane 口径与 _build_life_state 一致（进程级泳道，prod
    # 归一 "prod"）；render 空链 / 读失败返回 "" → 整段缺席、不塞占位。
    arc_awareness = await render_arc_awareness(
        lane=current_deployment_lane() or "prod"
    )
    if arc_awareness:
        sections.append(arc_awareness)

    scene = _scene_section(
        chat_type, chat_name, trigger_username, is_proactive, proactive_stimulus
    )
    if scene:
        sections.append(scene)

    sections.append(await _build_life_state(persona_id))

    return "\n\n".join(sections)
