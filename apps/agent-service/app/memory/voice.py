"""Unified voice generation — inner monologue + reply style in one LLM call.

Replaces the old ``voice_generator.py``.  Output is the full ``<voice>``
section injected into the system prompt.
"""

from __future__ import annotations

import logging

from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.data.queries import (
    insert_reply_style,
    list_today_fragments,
)
from app.domain.life_state import find_life_state
from app.infra import cst_time
from app.memory._persona import load_persona
from app.runtime.lane_policy import current_deployment_lane

_VOICE_CFG = AgentConfig("voice_generator", "offline-model", "voice-generator")

logger = logging.getLogger(__name__)


async def generate_voice(
    persona_id: str,
    recent_context: str = "",
    source: str = "cron",
) -> str | None:
    """Generate full voice content (inner monologue + style examples)."""
    pc = await load_persona(persona_id)
    # fallback: persona_id == display_name means persona not found
    if pc.display_name == persona_id and not pc.persona_lite:
        return None

    # lane 口径与 world/life 写入端一致：current_deployment_lane() or "prod"。
    lane = current_deployment_lane() or "prod"
    snap = await find_life_state(lane=lane, persona_id=persona_id)
    current_state = snap.current_state if snap else "（状态未知）"
    response_mood = snap.response_mood if snap else ""

    current_time = cst_time.to_cst_hm(cst_time.now_cst_iso())

    frags = await list_today_fragments(
        persona_id, sources=["afterthought"]
    )
    frag_text = (
        "\n".join(f.content[:100] for f in frags[-3:])
        if frags
        else "（今天还没跟人聊过）"
    )

    recent_ctx_block = ""
    if recent_context:
        recent_ctx_block = f"最近的对话和你的回复：\n{recent_context}"

    result = await Agent(_VOICE_CFG).run(
        prompt_vars={
            "persona_name": pc.display_name,
            "persona_lite": pc.persona_lite,
            "current_state": current_state,
            "response_mood": response_mood,
            "recent_fragments": frag_text,
            "recent_context": recent_ctx_block,
            "current_time": current_time,
        },
        messages=[Message(role=Role.USER, content="生成当前状态的内心独白和语气示例")],
    )

    content = result.text()

    if not content:
        logger.warning("[%s] Voice generation returned empty", persona_id)
        return None

    await insert_reply_style(
        persona_id=persona_id, style_text=content, source=source
    )
    logger.info("[%s] Voice generated (%s): %s...", persona_id, source, content[:60])
    return content
